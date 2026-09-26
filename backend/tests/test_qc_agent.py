"""质检 Agent 决策树的单元测试。

重点验证"本地不完全信任模型输出"这条设计原则：
模型说 pass 但本地阈值判定不合格时，以本地为准。
以及合规一票否决、置信度不足转人工、成本不对称下的保守倾向。
"""

from __future__ import annotations

import pytest

from app.agents.qc_agent import (
    CONFIDENCE_FLOOR,
    COST_FALSE_NEGATIVE_CENTS,
    COST_FALSE_POSITIVE_CENTS,
    DIMENSION_THRESHOLD,
    DIMENSIONS,
    QCAgent,
    QCResult,
    _extract_json,
    build_qc_user_prompt,
)
from app.core.errors import QCParseError
from app.models.enums import QCSeverity, QCSuggestion, QCVerdict


def _dims(**overrides: float) -> dict:
    """构造五维打分，默认全部合格（低分）。"""
    base = {d: 0.1 for d in DIMENSIONS}
    base.update(overrides)
    return {
        d: {"score": v, "ok": v < DIMENSION_THRESHOLD, "note": f"{d} note"} for d, v in base.items()
    }


def _raw(
    verdict: str = "pass",
    confidence: float = 0.9,
    dimensions: dict | None = None,
    **extra,
) -> dict:
    return {
        "pass": verdict == "pass",
        "verdict": verdict,
        "confidence": confidence,
        "dimensions": dimensions if dimensions is not None else _dims(),
        "severity": "low",
        "suggestion": None,
        "reasoning": "test",
        **extra,
    }


@pytest.fixture
def agent() -> QCAgent:
    # 不注入 vision provider：只测解析与决策树这部分纯逻辑
    return QCAgent(vision_provider=None)


# ==================== 基本判定 ====================


def test_all_dimensions_ok_returns_pass(agent: QCAgent) -> None:
    result = agent.parse_and_decide(_raw(verdict="pass", confidence=0.95))
    assert result.verdict is QCVerdict.PASS
    assert result.is_pass
    assert not result.needs_manual
    assert result.suggestion is None


def test_model_claims_pass_but_local_threshold_overrides(agent: QCAgent) -> None:
    """核心不变量：模型自报 pass 且自报 ok=True，但分数超阈值 → 以本地为准判不合格。

    这是"不把模型输出当可信结果"在质检域的具体体现。
    """
    dims = _dims(facial_deformity=0.9)  # 分数很高 = 问题严重
    dims["facial_deformity"]["ok"] = True  # 模型谎报合格
    dims["facial_deformity"]["note"] = "模型说没问题"

    result = agent.parse_and_decide(_raw(verdict="pass", confidence=0.95, dimensions=dims))

    assert result.verdict is not QCVerdict.PASS
    assert result.dimensions["facial_deformity"]["ok"] is False  # 本地重判覆盖模型自报


# ==================== 合规一票否决 ====================


def test_compliance_failure_always_blocks(agent: QCAgent) -> None:
    """合规命中必须 blocked，即使其他维度全好、模型说 pass。"""
    dims = _dims(compliance=0.8)
    result = agent.parse_and_decide(_raw(verdict="pass", confidence=0.99, dimensions=dims))

    assert result.verdict is QCVerdict.BLOCKED
    assert result.severity is QCSeverity.CRITICAL
    assert result.suggestion is QCSuggestion.MANUAL
    assert result.needs_manual


def test_model_verdict_blocked_is_respected(agent: QCAgent) -> None:
    """模型主动报 blocked 也要拦，即使本地各维度分数都低。"""
    result = agent.parse_and_decide(_raw(verdict="blocked", confidence=0.5))
    assert result.verdict is QCVerdict.BLOCKED


# ==================== 置信度不足转人工 ====================


def test_low_confidence_goes_manual_not_pass(agent: QCAgent) -> None:
    """不确定时不猜 pass——防止模型为了给出答案而强行判合格。"""
    result = agent.parse_and_decide(
        _raw(verdict="pass", confidence=CONFIDENCE_FLOOR - 0.01)
    )
    assert result.suggestion is QCSuggestion.MANUAL
    assert result.needs_manual
    assert result.verdict is not QCVerdict.PASS


def test_confidence_exactly_at_floor_passes(agent: QCAgent) -> None:
    """边界：等于阈值不算不足。"""
    result = agent.parse_and_decide(_raw(verdict="pass", confidence=CONFIDENCE_FLOOR))
    assert result.verdict is QCVerdict.PASS


# ==================== 修复动作路由 ====================


def test_identity_drift_suggests_strengthen_anchor(agent: QCAgent) -> None:
    """人设漂移 → 强化锚定，并带回漂移字段供第 3 级锚定使用。"""
    dims = _dims(identity_drift=0.75)
    dims["identity_drift"]["drifted_fields"] = ["发色", "眼距"]

    result = agent.parse_and_decide(_raw(verdict="repairable", dimensions=dims))

    assert result.suggestion is QCSuggestion.STRENGTHEN_ANCHOR
    assert result.drifted_fields == ["发色", "眼距"]
    assert result.verdict is QCVerdict.REPAIRABLE


def test_color_only_suggests_redraw(agent: QCAgent) -> None:
    """仅色彩问题 → 局部重绘，比整张重抽便宜。"""
    result = agent.parse_and_decide(
        _raw(verdict="repairable", dimensions=_dims(color_discontinuity=0.6))
    )
    assert result.suggestion is QCSuggestion.REDRAW


def test_facial_deformity_suggests_reseed(agent: QCAgent) -> None:
    result = agent.parse_and_decide(
        _raw(verdict="repairable", dimensions=_dims(facial_deformity=0.7))
    )
    assert result.suggestion is QCSuggestion.RESEED


def test_composition_suggests_reseed(agent: QCAgent) -> None:
    result = agent.parse_and_decide(
        _raw(verdict="repairable", dimensions=_dims(composition=0.65))
    )
    assert result.suggestion is QCSuggestion.RESEED


def test_drift_takes_priority_over_color(agent: QCAgent) -> None:
    """多维度不合格时，人设漂移优先（它说明锚定本身有缺陷，重绘治不了）。"""
    result = agent.parse_and_decide(
        _raw(
            verdict="repairable",
            dimensions=_dims(identity_drift=0.7, color_discontinuity=0.6),
        )
    )
    assert result.suggestion is QCSuggestion.STRENGTHEN_ANCHOR


def test_compliance_beats_everything(agent: QCAgent) -> None:
    """合规优先级高于所有其他维度，包括人设漂移。"""
    result = agent.parse_and_decide(
        _raw(
            verdict="repairable",
            dimensions=_dims(identity_drift=0.9, compliance=0.9),
        )
    )
    assert result.verdict is QCVerdict.BLOCKED


# ==================== 严重度分级 ====================


def test_severity_escalates_with_failure_count(agent: QCAgent) -> None:
    one = agent.parse_and_decide(_raw(dimensions=_dims(composition=0.6)))
    two = agent.parse_and_decide(
        _raw(dimensions=_dims(composition=0.6, color_discontinuity=0.6))
    )
    assert one.severity is QCSeverity.LOW
    assert two.severity is QCSeverity.MEDIUM


def test_deformity_plus_other_is_high(agent: QCAgent) -> None:
    result = agent.parse_and_decide(
        _raw(dimensions=_dims(facial_deformity=0.7, composition=0.6))
    )
    assert result.severity is QCSeverity.HIGH


# ==================== 解析健壮性 ====================


def test_extract_json_plain() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_markdown_fence() -> None:
    """模型常返回 ```json 包裹，必须能剥掉。"""
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_finds_embedded_object() -> None:
    """模型多嘴时，截取第一个 { 到最后一个 }。"""
    text = '好的，判定结果如下：{"verdict": "pass"} 希望有帮助'
    assert _extract_json(text) == {"verdict": "pass"}


def test_extract_json_raises_on_garbage() -> None:
    with pytest.raises(QCParseError):
        _extract_json("完全不是 JSON")


def test_extract_json_raises_on_empty() -> None:
    with pytest.raises(QCParseError):
        _extract_json("")


def test_missing_dimensions_raises(agent: QCAgent) -> None:
    with pytest.raises(QCParseError):
        agent.parse_and_decide({"verdict": "pass", "confidence": 0.9})


def test_non_dict_raises(agent: QCAgent) -> None:
    with pytest.raises(QCParseError):
        agent.parse_and_decide(["not", "a", "dict"])  # type: ignore[arg-type]


def test_malformed_score_coerced_to_zero(agent: QCAgent) -> None:
    """分数是字符串或 null 时不应崩，按 0（合格）处理。"""
    dims = _dims()
    dims["composition"]["score"] = None
    result = agent.parse_and_decide(_raw(dimensions=dims))
    assert result.dimensions["composition"]["score"] == 0.0


# ==================== Prompt 组装 ====================


def test_user_prompt_injects_anchor_version() -> None:
    """锚定版本必须进 Prompt 上下文，否则 Trace 无法归因。"""
    prompt = build_qc_user_prompt(
        anchor_prompt="角色[林晚] 面部: 脸型=鹅蛋脸",
        anchor_version=3,
        shot_size="中景",
        composition="主体居中",
        action_text="转身",
        dialogue=None,
        has_baseline_frame=True,
    )
    assert "v3" in prompt
    assert "林晚" in prompt
    assert "中景" in prompt


def test_baseline_frame_changes_prompt_wording() -> None:
    """有无基准帧，对色彩断层维度的判定要求不同，Prompt 必须区分。"""
    common = dict(
        anchor_prompt="x",
        anchor_version=1,
        shot_size="中景",
        composition="c",
        action_text="a",
        dialogue=None,
    )
    with_base = build_qc_user_prompt(has_baseline_frame=True, **common)
    without = build_qc_user_prompt(has_baseline_frame=False, **common)

    assert "第 2 张图" in with_base
    assert "从宽判定" in without
    assert with_base != without


# ==================== 成本不对称（阈值决策依据）====================


def test_false_negative_costs_more_than_false_positive() -> None:
    """漏放一张废片到视频层的损失，远大于误杀一张的重抽损失。

    这个比值是"阈值刻意调保守"的依据，面试要能说出具体数字。
    """
    assert COST_FALSE_NEGATIVE_CENTS > COST_FALSE_POSITIVE_CENTS
    ratio = COST_FALSE_NEGATIVE_CENTS / COST_FALSE_POSITIVE_CENTS
    assert ratio > 5  # 约 5.6 倍


def test_threshold_is_conservative() -> None:
    """阈值设在 0.4：宁可误杀不可漏放。"""
    assert 0 < DIMENSION_THRESHOLD < 0.5


def test_inspect_without_provider_raises() -> None:
    """未注入视觉 Provider 时必须显式报错，不能假装质检通过。"""
    agent = QCAgent(vision_provider=None)
    with pytest.raises(QCParseError):
        import asyncio

        asyncio.run(
            agent.inspect(
                image_paths=["a.png"],
                anchor_prompt="x",
                anchor_version=1,
                shot_size="中景",
                composition="c",
                action_text="a",
            )
        )


def test_qc_result_helpers() -> None:
    passed = QCResult(verdict=QCVerdict.PASS, confidence=0.9)
    assert passed.is_pass and not passed.needs_manual

    blocked = QCResult(verdict=QCVerdict.BLOCKED, suggestion=QCSuggestion.MANUAL)
    assert not blocked.is_pass and blocked.needs_manual
