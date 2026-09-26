"""角色一致性锚定：第 1 级（全局特征库固化）的程序化拼装。

核心原则：结构化字段 → 程序化拼装，禁止人手写 Prompt。
人手写会不自觉换措辞（"黑色长发" 这次写"及腰黑长直"、下次写"乌黑秀发"），
模型会把它们当不同角色。一致性要求逐字节相同的描述。

三条硬规则：
1. 禁用主观形容词（清秀/帅气/温柔/美丽）——模型每次解释都不同
2. 发色用色号（#1A1A1A 纯黑）——"黑色"在厚涂画风下会偏棕偏蓝
3. 负面提示词全局固定，所有镜头共用，保证画风收敛

锚定强度可调：质检返回 drifted_fields 时，下一轮只强化漂移字段，不整体重写。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 禁用词表：这些主观词会让锚定失效，检测到就告警
_SUBJECTIVE_WORDS = frozenset(
    {
        "清秀",
        "帅气",
        "漂亮",
        "美丽",
        "温柔",
        "可爱",
        "英俊",
        "迷人",
        "优雅",
        "冷酷",
        "阳光",
        "甜美",
        "性感",
        "好看",
        "俊美",
        "端庄",
    }
)

# 锚定强度等级：由质检反馈驱动升级
ANCHOR_LEVELS = ("normal", "strong", "strongest")


@dataclass
class AnchorInput:
    """角色结构化特征。对应 characters 表的 JSON 字段。"""

    name: str
    face_features: dict[str, str] = field(default_factory=dict)
    hair_features: dict[str, str] = field(default_factory=dict)
    body_features: dict[str, str] = field(default_factory=dict)
    outfit_features: dict[str, str] = field(default_factory=dict)
    style_lock: dict[str, str] = field(default_factory=dict)


def _check_subjective(features: dict[str, str]) -> list[str]:
    """检测主观词，返回命中的词。用于 CI 或建档时告警。"""
    hits: list[str] = []
    for value in features.values():
        for word in _SUBJECTIVE_WORDS:
            if word in str(value):
                hits.append(word)
    return hits


def _render_group(label: str, features: dict[str, str]) -> str:
    """把一个特征组渲染为 '标签: k=v, k=v' 形式，键顺序固定保证可复现。"""
    if not features:
        return ""
    items = ", ".join(f"{k}={features[k]}" for k in sorted(features))
    return f"{label}: {items}"


def build_anchor_prompt(
    data: AnchorInput,
    *,
    level: str = "normal",
    strengthen_fields: list[str] | None = None,
) -> str:
    """程序化拼装锚定 Prompt。

    参数：
      level: 锚定强度 normal/strong/strongest，由质检反馈升级
      strengthen_fields: 质检返回的 drifted_fields，只强化漂移的字段

    返回：逐字节稳定的锚定描述（同一输入 + 同一 level 必得同一输出）。
    """
    if level not in ANCHOR_LEVELS:
        logger.warning("未知锚定强度 %s，回退 normal", level)
        level = "normal"

    # 建档时检测主观词
    all_features = {
        **data.face_features,
        **data.hair_features,
        **data.body_features,
        **data.outfit_features,
    }
    subjective = _check_subjective(all_features)
    if subjective:
        logger.warning("角色 %s 特征含主观词 %s，建议改为客观可验证描述", data.name, subjective)

    groups = [
        _render_group("面部", data.face_features),
        _render_group("发型", data.hair_features),
        _render_group("体型", data.body_features),
        _render_group("服饰", data.outfit_features),
    ]
    body = "; ".join(g for g in groups if g)

    style = data.style_lock or {}
    art_style = style.get("画风", "")
    tone = style.get("全局色调", "")

    prompt = f"角色[{data.name}] {body}"
    if art_style:
        prompt += f"; 画风: {art_style}"
    if tone:
        prompt += f"; 色调: {tone}"

    # 强度升级：强化漂移字段
    if level in ("strong", "strongest") and strengthen_fields:
        emphasis = _build_emphasis(data, strengthen_fields)
        if emphasis:
            # strong: 漂移字段前置 + 重复一次；strongest: 再加负面约束
            prompt = f"[必须严格保持] {emphasis}。{prompt}"
            if level == "strongest":
                neg = _build_negative_from_drift(strengthen_fields)
                if neg:
                    prompt += f"。[严禁] {neg}"

    return prompt


def _find_field(data: AnchorInput, field_key: str) -> str | None:
    """在所有特征组里找到某个字段的值。"""
    for features in (
        data.face_features,
        data.hair_features,
        data.body_features,
        data.outfit_features,
    ):
        if field_key in features:
            return f"{field_key}={features[field_key]}"
    return None


def _build_emphasis(data: AnchorInput, fields: list[str]) -> str:
    parts = [p for f in fields if (p := _find_field(data, f))]
    return ", ".join(parts)


def _build_negative_from_drift(fields: list[str]) -> str:
    """根据漂移字段生成负面约束。如发色漂移 → 禁止其他发色。"""
    negatives: list[str] = []
    for f in fields:
        if "hair_color" in f or "发色" in f:
            negatives.append("非设定发色")
        elif "eye" in f or "眼" in f:
            negatives.append("眼型偏差")
        elif "outfit" in f or "服" in f:
            negatives.append("服饰款式偏差")
    return ", ".join(negatives)


def build_negative_prompt(project_negative: str | None, style_lock: dict[str, str] | None) -> str:
    """合并全局负面提示词 + 画风负面词。所有镜头共用，保证画风收敛。"""
    parts: list[str] = []
    if project_negative:
        parts.append(project_negative.strip())
    if style_lock and style_lock.get("负面提示词"):
        parts.append(style_lock["负面提示词"].strip())
    # 去重保序
    seen: set[str] = set()
    merged: list[str] = []
    for p in parts:
        for token in p.split(","):
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                merged.append(token)
    return ", ".join(merged)


def next_anchor_level(current: str, verdict_suggestion: str | None) -> str:
    """根据质检建议决定下一轮锚定强度。

    决策树（见 docs/03）：
      strengthen_anchor → 升一级
      已是 strongest 仍漂移 → 转人工（返回 None 由调用方处理）
    """
    if verdict_suggestion != "strengthen_anchor":
        return current
    idx = ANCHOR_LEVELS.index(current) if current in ANCHOR_LEVELS else 0
    if idx >= len(ANCHOR_LEVELS) - 1:
        return current  # 已最高级，调用方应转人工检查基准图
    return ANCHOR_LEVELS[idx + 1]
