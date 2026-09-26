"""角色锚定拼装的单元测试。

覆盖三级锚定第 1 级的关键不变量：
- 逐字节稳定性（同输入同输出，这是一致性的前提）
- 主观词检测
- 强度升级只强化漂移字段，不整体重写
"""

from __future__ import annotations

import pytest

from app.knowledge.anchor import (
    ANCHOR_LEVELS,
    AnchorInput,
    build_anchor_prompt,
    build_negative_prompt,
    next_anchor_level,
)


@pytest.fixture
def sample_character() -> AnchorInput:
    """一个特征完整、无主观词的角色。"""
    return AnchorInput(
        name="林晚",
        face_features={"脸型": "鹅蛋脸", "眼距": "标准", "眼型": "丹凤眼", "标记": "左眼下泪痣"},
        hair_features={"发色": "#1A1A1A 纯黑", "长度": "及腰", "发型": "低马尾"},
        body_features={"身高": "168cm", "体型": "纤细"},
        outfit_features={"款式": "改良汉服交领", "配色": "月白主色+靛蓝滚边"},
        style_lock={"画风": "日系厚涂", "全局色调": "低饱和冷色", "负面提示词": "3D渲染, 塑料感"},
    )


def test_anchor_prompt_is_deterministic(sample_character: AnchorInput) -> None:
    """同一输入必须产出逐字节相同的 Prompt。

    这是角色一致性的根本前提：如果每次拼装结果不同，模型就会把同一角色
    当成不同角色，跨镜头必然崩脸。
    """
    first = build_anchor_prompt(sample_character)
    for _ in range(5):
        assert build_anchor_prompt(sample_character) == first


def test_anchor_prompt_contains_all_features(sample_character: AnchorInput) -> None:
    prompt = build_anchor_prompt(sample_character)
    assert "林晚" in prompt
    assert "鹅蛋脸" in prompt
    assert "#1A1A1A 纯黑" in prompt
    assert "改良汉服交领" in prompt
    assert "日系厚涂" in prompt


def test_field_order_is_stable_regardless_of_dict_order() -> None:
    """特征字典的插入顺序不同，产出必须相同（排序键保证可复现）。"""
    a = AnchorInput(name="X", face_features={"眼型": "丹凤眼", "脸型": "鹅蛋脸"})
    b = AnchorInput(name="X", face_features={"脸型": "鹅蛋脸", "眼型": "丹凤眼"})
    assert build_anchor_prompt(a) == build_anchor_prompt(b)


def test_subjective_words_are_detected(caplog: pytest.LogCaptureFixture) -> None:
    """含主观词的角色应触发告警——这些词会让锚定失效。"""
    vague = AnchorInput(name="路人", face_features={"气质": "清秀温柔"})
    with caplog.at_level("WARNING"):
        build_anchor_prompt(vague)
    assert "主观词" in caplog.text


def test_strong_level_prepends_drifted_fields(sample_character: AnchorInput) -> None:
    """强化锚定时，漂移字段要前置强调。"""
    normal = build_anchor_prompt(sample_character, level="normal")
    strong = build_anchor_prompt(
        sample_character, level="strong", strengthen_fields=["发色"]
    )
    assert strong != normal
    assert strong.startswith("[必须严格保持]")
    # 前置强调里应含漂移字段的值
    assert "#1A1A1A 纯黑" in strong.split("。")[0]


def test_strongest_level_adds_negative_constraint(sample_character: AnchorInput) -> None:
    """最高强度要追加负面约束（如禁止非设定发色）。"""
    strongest = build_anchor_prompt(
        sample_character, level="strongest", strengthen_fields=["发色"]
    )
    assert "[严禁]" in strongest
    assert "非设定发色" in strongest


def test_unknown_level_falls_back_to_normal(
    sample_character: AnchorInput, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        result = build_anchor_prompt(sample_character, level="nonsense")
    assert result == build_anchor_prompt(sample_character, level="normal")


def test_next_anchor_level_progression() -> None:
    """只有 strengthen_anchor 建议才升级强度。"""
    assert next_anchor_level("normal", "strengthen_anchor") == "strong"
    assert next_anchor_level("strong", "strengthen_anchor") == "strongest"
    # 已最高级不再升（调用方应转人工检查基准图）
    assert next_anchor_level("strongest", "strengthen_anchor") == "strongest"
    # 其他建议不改强度
    assert next_anchor_level("normal", "reseed") == "normal"
    assert next_anchor_level("strong", None) == "strong"


def test_negative_prompt_merges_and_dedupes() -> None:
    """全局负面词与画风负面词合并去重，保持顺序。"""
    merged = build_negative_prompt(
        "模糊, 低质量", {"负面提示词": "3D渲染, 模糊, 塑料感"}
    )
    parts = [p.strip() for p in merged.split(",")]
    assert parts.count("模糊") == 1  # 去重
    assert "3D渲染" in parts
    assert "低质量" in parts


def test_anchor_levels_constant() -> None:
    assert ANCHOR_LEVELS == ("normal", "strong", "strongest")


def test_empty_character_still_produces_prompt() -> None:
    """特征全空也不应抛异常，只产出角色名——建档草稿阶段会用到。"""
    empty = AnchorInput(name="待定角色")
    prompt = build_anchor_prompt(empty)
    assert "待定角色" in prompt
