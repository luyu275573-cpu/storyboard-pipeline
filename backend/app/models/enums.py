"""领域枚举：状态机的合法取值集中定义，避免字符串散落各处拼错。"""

from __future__ import annotations

from enum import StrEnum


class ShotStatus(StrEnum):
    """镜头状态，对应流水线阶段。"""

    PENDING = "pending"
    STORYBOARDED = "storyboarded"  # 分镜已生成
    RENDERING = "rendering"  # 抽卡中
    QC = "qc"  # 质检中
    REVIEW = "review"  # 待人工终审
    VIDEO = "video"  # 视频生成中
    SYNTHESIZED = "synthesized"  # 已合成
    FAILED = "failed"
    SUSPENDED = "suspended"  # 熔断/超上限，挂起待人工


class RunStatus(StrEnum):
    """流水线运行实例状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_GATE = "waiting_gate"  # 卡在人机关卡
    SUSPENDED = "suspended"  # 预算熔断或重试超限
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class RenderStage(StrEnum):
    """抽卡阶段：图像层 or 视频层。"""

    IMAGE = "image"
    VIDEO = "video"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELED = "canceled"


class QCVerdict(StrEnum):
    """质检判定结论。"""

    PASS = "pass"  # noqa: S105 -- 质检状态，不是口令
    REPAIRABLE = "repairable"  # 可自动修复
    REJECT = "reject"  # 不合格，重抽计数 +1
    BLOCKED = "blocked"  # 合规拦截，禁止自动重试


class QCSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class QCSuggestion(StrEnum):
    """质检给出的自动修复动作，与 QCVerdict.REPAIRABLE 配套。"""

    RESEED = "reseed"  # 同 Prompt 换种子
    REDRAW = "redraw"  # 局部重绘
    STRENGTHEN_ANCHOR = "strengthen_anchor"  # 强化锚定
    MANUAL = "manual"  # 转人工


class QCDimension(StrEnum):
    """质检五维度，对齐行业六大维度中图像层可判定的部分。"""

    FACIAL_DEFORMITY = "facial_deformity"  # 五官畸形
    IDENTITY_DRIFT = "identity_drift"  # 人设漂移
    COLOR_DISCONTINUITY = "color_discontinuity"  # 色彩断层
    COMPOSITION = "composition"  # 构图合规
    COMPLIANCE = "compliance"  # 内容合规（一票否决）


class GoldenLabel(StrEnum):
    """黄金测试集人工标注标签。"""

    PASS = "pass"  # noqa: S105 -- 标注状态，不是口令
    FACIAL_DEFORMITY = "facial_deformity"
    IDENTITY_DRIFT = "identity_drift"
    COLOR_DISCONTINUITY = "color_discontinuity"
    COMPOSITION = "composition"
    COMPLIANCE = "compliance"


class ProviderKind(StrEnum):
    """Provider 类别，用于路由与记账。"""

    IMAGE = "image"
    VIDEO = "video"
    VISION = "vision"
    LLM = "llm"


class GateType(StrEnum):
    """三道强制人机关卡。"""

    CHARACTER = "character"  # A：角色设定确认
    STORYBOARD = "storyboard"  # B：分镜脚本审核
    COMPLIANCE = "compliance"  # C：先审后播合规终审


class GateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class BudgetScope(StrEnum):
    """三级预算：项目 / 镜头 / 类型。"""

    PROJECT = "project"
    SHOT = "shot"
    KIND = "kind"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
