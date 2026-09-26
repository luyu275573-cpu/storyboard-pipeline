"""质检 Agent：漫剧生产流水线的核心差异化能力。

设计要点（详见 docs/03-质检Agent与评估.md）：
1. 输出结构化 JSON，不是自然语言——可复现、可统计、可与人工标注做混淆矩阵
2. 注入同场景已合格帧作为视觉基准——色彩断层与画风漂移必须靠视觉比对，纯文字判不出
3. "不确定就转人工"——防止模型为了给出答案而强行判 pass
   （与农牧项目"不将 Prompt 作为安全边界"同一思路：不信任模型的自信）
4. 合规维度一票否决，禁止自动重试——对应"先审后播"监管要求
5. drifted_fields 映射回角色特征字段，形成闭环驱动锚定强化
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any

from app.core.config import settings
from app.core.errors import QCParseError
from app.models.enums import ProviderKind, QCSeverity, QCSuggestion, QCVerdict
from app.providers.base import CallContext, ProviderRouter

logger = logging.getLogger(__name__)

# 五维度定义：图像层可判定的部分（时序抖动需视频，只做抽检）
DIMENSIONS: tuple[str, ...] = (
    "facial_deformity",
    "identity_drift",
    "color_discontinuity",
    "composition",
    "compliance",
)

DIMENSION_LABELS: dict[str, str] = {
    "facial_deformity": "五官畸形",
    "identity_drift": "人设漂移",
    "color_discontinuity": "色彩断层",
    "composition": "构图合规",
    "compliance": "内容合规",
}

# 判定阈值：低于该分视为该维度不合格
# 刻意调保守（宁可误杀不可漏放）：漏放一张废片到视频层损失约 1.4 元，
# 误杀一张只损失约 0.25 元，成本比约 5.6 倍
DIMENSION_THRESHOLD = 0.40
CONFIDENCE_FLOOR = 0.60  # 低于此置信度一律转人工，不猜 pass

# 漏放/误杀的成本（分），用于阈值决策与报表
COST_FALSE_NEGATIVE_CENTS = 140  # 废片流到视频层
COST_FALSE_POSITIVE_CENTS = 25  # 好帧被重抽


@dataclass
class QCResult:
    """结构化判定结果。"""

    verdict: QCVerdict
    dimensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    severity: QCSeverity | None = None
    suggestion: QCSuggestion | None = None
    confidence: float = 0.0
    reasoning: str = ""
    drifted_fields: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pass(self) -> bool:
        return self.verdict is QCVerdict.PASS

    @property
    def needs_manual(self) -> bool:
        return self.verdict is QCVerdict.BLOCKED or self.suggestion is QCSuggestion.MANUAL


QC_SYSTEM_PROMPT = """你是漫剧生产流水线的质检员，判定关键帧是否可用于成片。

## 判定维度（逐项打分，0-1，分数越高表示问题越严重）
1. facial_deformity 五官畸形：眼鼻嘴错位、多指少指、肢体扭曲、面部融化
2. identity_drift 人设漂移：与角色特征库逐项比对，脸型/眼距/发型/发色/服饰/配色是否一致；
   若漂移，必须在 drifted_fields 中列出具体漂移的字段名（用特征库里的键名）
3. color_discontinuity 色彩断层：与同场景基准帧比对色调/光照/画风是否一致
4. composition 构图合规：景别是否符合分镜要求、主体是否出画、是否给台词字幕留白
5. compliance 内容合规：违规元素、侵权风险；命中即一票否决

## 硬约束
- 只依据可见画面判定，不推测、不脑补画面外内容
- 不确定时降低 confidence 并给出 manual 建议，不要猜 pass
- compliance 命中时 verdict 必须是 blocked
- 严格输出 JSON，不要输出任何解释性文字或 markdown 代码块

## 输出 JSON schema
{
  "pass": <bool>,
  "verdict": "pass" | "repairable" | "reject" | "blocked",
  "confidence": <0-1 float>,
  "dimensions": {
    "facial_deformity":    {"score": <0-1>, "ok": <bool>, "note": "<简述>"},
    "identity_drift":      {"score": <0-1>, "ok": <bool>, "note": "<简述>",
                            "drifted_fields": ["<特征库字段名>"]},
    "color_discontinuity": {"score": <0-1>, "ok": <bool>, "note": "<简述>"},
    "composition":         {"score": <0-1>, "ok": <bool>, "note": "<简述>"},
    "compliance":          {"score": <0-1>, "ok": <bool>, "note": "<简述>"}
  },
  "severity": "low" | "medium" | "high" | "critical",
  "suggestion": "reseed" | "redraw" | "strengthen_anchor" | "manual",
  "reasoning": "<一句话说明判定依据>"
}
"""


def build_qc_user_prompt(
    *,
    anchor_prompt: str,
    anchor_version: int,
    shot_size: str,
    composition: str,
    action_text: str,
    dialogue: str | None,
    has_baseline_frame: bool,
) -> str:
    """组装质检输入。

    注入角色锚定（含版本号，便于 Trace 归因）+ 分镜要求 + 是否附带基准帧。
    """
    parts = [
        f"## 角色特征库（锚定版本 v{anchor_version}）",
        anchor_prompt or "（未提供角色特征）",
        "",
        "## 本镜头分镜要求",
        f"- 景别: {shot_size}",
        f"- 构图: {composition}",
        f"- 动作: {action_text}",
        f"- 台词: {dialogue or '（无）'}",
        "",
        "## 图像输入说明",
    ]
    if has_baseline_frame:
        parts.append("第 1 张图为【同场景已合格基准帧】，用于比对色调/光照/画风；")
        parts.append("第 2 张图为【待判定关键帧】。")
    else:
        parts.append("仅 1 张图，为【待判定关键帧】；本场景尚无基准帧，色彩断层维度请从宽判定。")
    return "\n".join(parts)


def _parse_operation_key(base: str, attempt: int) -> str:
    suffix = f":parse-{attempt}"
    return f"{base[: 120 - len(suffix)]}{suffix}"


class QCAgent:
    """质检判定与决策树。

    视觉模型调用通过 VisionProvider 注入，便于测试时用假 Provider 替换。
    """

    def __init__(
        self,
        vision_provider: Any | None = None,
        *,
        provider_router: ProviderRouter | None = None,
        parse_max_retry: int = 2,
    ) -> None:
        self.vision = vision_provider
        self.router = provider_router
        self.parse_max_retry = parse_max_retry

    # ---------- 判定 ----------

    async def inspect(
        self,
        *,
        image_paths: list[str],
        anchor_prompt: str,
        anchor_version: int,
        shot_size: str,
        composition: str,
        action_text: str,
        dialogue: str | None = None,
        has_baseline_frame: bool = False,
        call_context: CallContext | None = None,
        model: str | None = None,
    ) -> QCResult:
        """对一张关键帧做五维判定。

        image_paths: [基准帧?, 待判定帧]
        """
        if self.router is None and self.vision is None:
            msg = "VisionProvider 未注入，无法执行质检"
            raise QCParseError(msg)
        if self.router is not None and call_context is None:
            raise QCParseError("统一视觉路由必须提供 CallContext")

        user_prompt = build_qc_user_prompt(
            anchor_prompt=anchor_prompt,
            anchor_version=anchor_version,
            shot_size=shot_size,
            composition=composition,
            action_text=action_text,
            dialogue=dialogue,
            has_baseline_frame=has_baseline_frame,
        )

        # 解析失败重试：结构化输出偶尔会返回带 markdown 包裹或多余文字
        last_error: Exception | None = None
        for attempt in range(1, self.parse_max_retry + 2):
            if self.router is not None:
                assert call_context is not None
                # 每次解析尝试都是一次实际供应商调用，独立记账且可单独重放。
                operation_key = _parse_operation_key(call_context.operation_key, attempt)
                retry_context = replace(
                    call_context, kind=ProviderKind.VISION, operation_key=operation_key[:120]
                )
                result = await self.router.generate(
                    ProviderKind.VISION,
                    {
                        "model": model or settings.siliconflow_vision_model,
                        "system_prompt": QC_SYSTEM_PROMPT,
                        "user_prompt": user_prompt,
                        "image_paths": image_paths,
                    },
                    retry_context,
                )
            else:
                assert self.vision is not None
                result = await self.vision.judge(
                    system_prompt=QC_SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    image_paths=image_paths,
                )
            try:
                return self.parse_and_decide(result.parsed or _extract_json(result.text or ""))
            except (QCParseError, ValueError, KeyError) as exc:
                last_error = exc
                logger.warning("质检结果解析失败（第 %d 次）: %s", attempt, exc)

        msg = f"质检结果连续 {self.parse_max_retry + 1} 次解析失败"
        raise QCParseError(msg, detail={"last_error": str(last_error)})

    # ---------- 解析与决策 ----------

    def parse_and_decide(self, raw: dict[str, Any]) -> QCResult:
        """把模型输出转成结构化判定，并施加本地决策规则。

        关键：不完全信任模型的 verdict。模型说 pass，但本地阈值判定不合格，
        以本地为准——这是"不把模型输出当可信结果"在质检域的具体体现。
        """
        if not isinstance(raw, dict):
            msg = "质检返回不是 JSON 对象"
            raise QCParseError(msg)

        dims_raw = raw.get("dimensions")
        if not isinstance(dims_raw, dict):
            msg = "质检返回缺少 dimensions 字段"
            raise QCParseError(msg)

        dimensions: dict[str, dict[str, Any]] = {}
        drifted: list[str] = []
        failed_dims: list[str] = []

        for dim in DIMENSIONS:
            entry = dims_raw.get(dim)
            if not isinstance(entry, dict):
                msg = f"维度 {dim} 格式非法"
                raise QCParseError(msg)
            score = _to_float(entry.get("score"))
            # 本地重判：不信任模型自报的 ok
            ok = score < DIMENSION_THRESHOLD
            note = str(entry.get("note") or "")
            dimensions[dim] = {"score": score, "ok": ok, "note": note}

            if dim == "identity_drift":
                fields = entry.get("drifted_fields") or []
                if isinstance(fields, list):
                    drifted = [str(f) for f in fields]
                    dimensions[dim]["drifted_fields"] = drifted

            if not ok:
                failed_dims.append(dim)

        confidence = _to_float(raw.get("confidence"))
        model_verdict = str(raw.get("verdict") or "").strip().lower()
        reasoning = str(raw.get("reasoning") or "")

        verdict, severity, suggestion = self._decide(
            failed_dims=failed_dims,
            confidence=confidence,
            model_verdict=model_verdict,
        )

        return QCResult(
            verdict=verdict,
            dimensions=dimensions,
            severity=severity,
            suggestion=suggestion,
            confidence=confidence,
            reasoning=reasoning,
            drifted_fields=drifted,
            raw=raw,
        )

    def _decide(
        self,
        *,
        failed_dims: list[str],
        confidence: float,
        model_verdict: str,
    ) -> tuple[QCVerdict, QCSeverity | None, QCSuggestion | None]:
        """本地决策树。

        优先级：合规拦截 > 置信度不足转人工 > 无不合格则通过 > 按维度决定修复动作
        """
        # 1. 合规一票否决：强制人工，禁止自动重试
        if "compliance" in failed_dims or model_verdict == "blocked":
            return QCVerdict.BLOCKED, QCSeverity.CRITICAL, QCSuggestion.MANUAL

        # 2. 置信度不足：不猜 pass，转人工
        if confidence < CONFIDENCE_FLOOR:
            logger.info("质检置信度 %.2f 低于阈值 %.2f，转人工", confidence, CONFIDENCE_FLOOR)
            return QCVerdict.REPAIRABLE, QCSeverity.MEDIUM, QCSuggestion.MANUAL

        # 3. 全部维度合格
        if not failed_dims:
            return QCVerdict.PASS, None, None

        # 4. 按维度决定修复动作
        severity = self._severity_of(failed_dims)

        if "identity_drift" in failed_dims:
            # 人设漂移 → 强化锚定（drifted_fields 会驱动只强化漂移字段）
            return QCVerdict.REPAIRABLE, severity, QCSuggestion.STRENGTHEN_ANCHOR
        if "color_discontinuity" in failed_dims and len(failed_dims) == 1:
            # 仅色彩问题 → 局部重绘或调色，比整张重抽便宜
            return QCVerdict.REPAIRABLE, severity, QCSuggestion.REDRAW
        if "facial_deformity" in failed_dims or "composition" in failed_dims:
            # 畸形或构图问题 → 换种子重抽
            return QCVerdict.REPAIRABLE, severity, QCSuggestion.RESEED

        return QCVerdict.REJECT, severity, QCSuggestion.RESEED

    @staticmethod
    def _severity_of(failed_dims: list[str]) -> QCSeverity:
        if "facial_deformity" in failed_dims and len(failed_dims) >= 2:
            return QCSeverity.HIGH
        if len(failed_dims) >= 3:
            return QCSeverity.HIGH
        if len(failed_dims) == 2:
            return QCSeverity.MEDIUM
        return QCSeverity.LOW


def _to_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QCParseError("质检分数和置信度必须为 0 到 1 的有限数值")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise QCParseError("质检分数和置信度超出有效范围")
    return score


def _extract_json(text: str) -> dict[str, Any]:
    """从可能带 markdown 包裹的文本里提取 JSON。降级路径。"""
    if not text:
        msg = "质检返回为空"
        raise QCParseError(msg)

    cleaned = text.strip()
    # 去掉 ```json ... ``` 包裹
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 退一步：截取第一个 { 到最后一个 }
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            msg = f"质检返回无法解析为 JSON: {exc}"
            raise QCParseError(msg) from exc

    msg = "质检返回中未找到 JSON 对象"
    raise QCParseError(msg)
