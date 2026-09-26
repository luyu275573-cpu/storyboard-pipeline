"""知识库包：角色特征库与锚定 Prompt 组装。"""

from app.knowledge.anchor import (
    ANCHOR_LEVELS,
    AnchorInput,
    build_anchor_prompt,
    build_negative_prompt,
    next_anchor_level,
)

__all__ = [
    "ANCHOR_LEVELS",
    "AnchorInput",
    "build_anchor_prompt",
    "build_negative_prompt",
    "next_anchor_level",
]
