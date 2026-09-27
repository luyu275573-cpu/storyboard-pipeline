"""请求/响应模型包。

约定：所有入参出参必须经 Pydantic 校验，Controller 不直接接收/返回 ORM 对象。
Flask 项目里手动接 Pydantic 是加分项，FastAPI 里则是框架原生能力，务必用足。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class ORMModel(BaseModel):
    """ORM 出参基类。"""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """分页包装。避免大 OFFSET，超过阈值应改游标分页。"""

    items: list[T]
    total: int
    page: int = 1
    page_size: int = 20


# ==================== 项目 ====================


class ProjectCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    synopsis: str | None = Field(default=None, max_length=15000)
    style: str = Field(default="日系厚涂", min_length=1, max_length=80)
    global_negative_prompt: str | None = Field(default=None, max_length=2000)
    # 不传则用配置里的总预算
    budget_cents: int | None = Field(default=None, ge=0, le=20000)


class ProjectOut(ORMModel):
    id: str
    title: str
    synopsis: str | None
    style: str
    global_negative_prompt: str | None
    status: str
    budget_cents: int
    storyboard_version: int
    created_at: datetime
    updated_at: datetime


# ==================== 角色与锚定 ====================


class CharacterCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str
    name: str = Field(min_length=1, max_length=80)
    # 结构化特征：只接受客观可验证描述，主观词会在锚定拼装时告警
    face_features: dict[str, str] = Field(default_factory=dict)
    hair_features: dict[str, str] = Field(default_factory=dict)
    body_features: dict[str, str] = Field(default_factory=dict)
    outfit_features: dict[str, str] = Field(default_factory=dict)
    style_lock: dict[str, str] = Field(default_factory=dict)

    @field_validator("face_features", "hair_features", "body_features", "outfit_features", "style_lock")
    @classmethod
    def validate_features(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 30 or any(
            not k.strip() or not v.strip() or len(k) > 80 or len(v) > 500 for k, v in value.items()
        ):
            raise ValueError("每组最多 30 项；名称和描述不可空，分别不超过 80/500 字")
        return value


class CharacterUpdate(CharacterCreate):
    expected_version: int = Field(ge=1)


class CharacterConfirm(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=36)
    anchor_version: int = Field(ge=1)
    reviewer: str = Field(min_length=1, max_length=80)


class AnchorPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["normal", "strong", "strongest"] = "normal"
    strengthen_fields: list[str] = Field(default_factory=list, max_length=30)


class CharacterOut(ORMModel):
    id: str
    project_id: str
    name: str
    face_features: dict[str, Any]
    hair_features: dict[str, Any]
    body_features: dict[str, Any]
    outfit_features: dict[str, Any]
    style_lock: dict[str, Any]
    anchor_prompt: str
    anchor_version: int
    confirmed: bool
    confirmed_at: datetime | None


class ReferenceReview(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=36)
    anchor_version: int = Field(ge=1)
    expected_review_version: int = Field(ge=0)
    passed: bool
    is_primary: bool = False
    reviewer: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2000)


class CharacterRefOut(ORMModel):
    id: str
    character_id: str
    ref_type: str
    asset_path: str
    asset_sha256: str | None
    ref_weight: float
    qc_passed: bool
    is_primary: bool
    uploaded_anchor_version: int
    reviewed_anchor_version: int | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    review_version: int


class AnchorPreview(BaseModel):
    """锚定 Prompt 预览：建档时先看拼装结果，避免主观词与缺字段进生产线。"""

    character_id: str
    anchor_version: int
    level: str
    anchor_prompt: str
    negative_prompt: str
    subjective_word_hits: list[str] = Field(default_factory=list)


# ==================== 分镜镜头 ====================


class SceneCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    project_id: str = Field(min_length=1, max_length=36)
    seq: int = Field(ge=1, le=10000)
    location: str = Field(min_length=1, max_length=200)
    time_of_day: str = Field(default="day", min_length=1, max_length=40)
    mood: str = Field(default="", max_length=80)
    background_prompt: str | None = Field(default=None, max_length=4000)


class SceneUpdate(SceneCreate):
    expected_storyboard_version: int = Field(ge=1)


class SceneOut(ORMModel):
    id: str
    project_id: str
    seq: int
    location: str
    time_of_day: str
    mood: str
    background_prompt: str | None


class ShotCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    scene_id: str = Field(min_length=1, max_length=36)
    seq: int = Field(ge=1, le=10000)
    shot_size: Literal["特写", "近景", "中景", "全景", "远景"] = "中景"
    camera_move: str | None = Field(default=None, max_length=40)
    composition: str = Field(min_length=1, max_length=4000)
    character_ids: list[str] = Field(default_factory=list, max_length=20)
    action_text: str = Field(min_length=1, max_length=4000)
    dialogue: str | None = Field(default=None, max_length=4000)
    duration_ms: int = Field(default=5000, ge=500, le=60000)
    negative_prompt: str | None = Field(default=None, max_length=2000)

    @field_validator("character_ids")
    @classmethod
    def unique_characters(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("镜头中的角色不可重复")
        return value


class ShotUpdate(ShotCreate):
    expected_version: int = Field(ge=1)


class StoryboardDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=36)
    storyboard_version: int = Field(ge=1)
    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2000)


class ShotOut(ORMModel):
    id: str
    scene_id: str
    seq: int
    shot_size: str
    camera_move: str | None
    composition: str
    character_ids: list[Any]
    action_text: str
    dialogue: str | None
    duration_ms: int
    negative_prompt: str | None
    status: str
    retry_count: int
    max_retry: int
    locked_attempt_id: str | None
    accepted_video_attempt_id: str | None
    prev_locked_attempt_id: str | None
    version: int


class RenderRequest(BaseModel):
    """触发抽卡。n 张并行抽，由质检挑合格帧。"""

    n: int = Field(default=2, ge=1, le=6)
    # 不传则按当前锚定等级；质检驱动重抽时由系统传入 strengthen_anchor
    anchor_level: str | None = Field(default=None, description="normal/strong/strongest")
    strengthen_fields: list[str] = Field(default_factory=list)
    stage: str = Field(default="image", description="image/video")


class AttemptOut(ORMModel):
    id: str
    shot_id: str
    attempt_no: int
    stage: str
    provider: str
    model: str
    seed: str | None
    anchor_version: int | None
    asset_path: str | None
    asset_sha256: str | None
    status: str
    error_code: str | None
    cost_cents: int
    latency_ms: int | None
    created_at: datetime


# ==================== 质检 ====================


class QCInspectRequest(BaseModel):
    attempt_id: str
    # 同场景基准帧，用于色彩断层与画风比对；无则从宽判定
    baseline_attempt_id: str | None = None


class QCReportOut(ORMModel):
    id: str
    attempt_id: str
    model: str
    verdict: str
    dimensions: dict[str, Any]
    severity: str | None
    suggestion: str | None
    confidence: float | None
    reasoning: str | None
    human_verdict: str | None
    human_note: str | None
    reviewed_at: datetime | None
    created_at: datetime


class GoldenLabelCreate(BaseModel):
    """人工标注：黄金测试集的数据来源。"""

    asset_key: str = Field(min_length=64, max_length=64, description="素材内容 sha256")
    asset_path: str
    character_id: str | None = None
    label: str = Field(
        description="pass/facial_deformity/identity_drift/color_discontinuity/composition/compliance"
    )
    label_detail: dict[str, Any] | None = None
    labeled_by: str = "manual"


class QCReviewRequest(BaseModel):
    """人工复核质检结论：用于计算混淆矩阵。"""

    human_verdict: str = Field(description="pass/reject")
    human_note: str | None = None


class ConfusionMatrix(BaseModel):
    """黄金测试集上的混淆矩阵与业务指标。

    两类错误成本不对称，必须分开看：
      漏放(FN)：废片流到视频层，损失约 1.4 元
      误杀(FP)：好帧被重抽，损失约 0.25 元
    """

    total: int
    tp: int
    tn: int
    fp: int
    fn: int
    accuracy: float
    recall: float
    false_positive_rate: float
    cost_fn_cents: int
    cost_fp_cents: int
    # 各维度不合格占比：错误分类体系，反向驱动优化方向
    dimension_breakdown: dict[str, int] = Field(default_factory=dict)


# ==================== 预算与成本 ====================


class BudgetOut(BaseModel):
    project_id: str
    budget_cents: int
    spent_cents: int
    remaining_cents: int
    ratio: float
    warn_ratio: float
    degrade_ratio: float
    per_shot_cents: int
    shot_max_retry: int


class CostReport(BaseModel):
    """简历量化指标的数据源。"""

    project_id: str
    total_spent_cents: int
    total_calls: int
    success_calls: int
    shot_cost: list[dict[str, Any]]
    provider_stats: list[dict[str, Any]]
    qc_savings: dict[str, Any]
    first_pass_rate: float | None = Field(default=None, description="一次过合格率：attempt_no=1 即合格的比例")


# ==================== 流水线与人机关卡 ====================


class GateDecision(BaseModel):
    """人机关卡放行/驳回。三道关卡均不可跳过。"""

    status: Literal["approved", "rejected"]
    reviewer: str | None = None
    note: str | None = None
    attempt_id: str | None = Field(default=None, max_length=36)
    expected_version: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, max_length=36)


class MediaDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=36)
    attempt_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2000)


class ManualQCDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    run_id: str = Field(min_length=1, max_length=36)
    attempt_id: str = Field(min_length=1, max_length=36)
    expected_version: int = Field(ge=1)
    status: Literal["pass", "reject"]
    reviewer: str = Field(min_length=1, max_length=80)
    note: str = Field(min_length=1, max_length=2000)


class GateOut(ORMModel):
    id: str
    run_id: str
    gate_type: str
    status: str
    reviewer: str | None
    note: str | None
    snapshot: dict[str, Any]
    decided_at: datetime | None
    created_at: datetime


class RunOut(ORMModel):
    id: str
    project_id: str
    status: str
    current_stage: str | None
    graph_state: dict[str, Any]
    spent_cents: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ProgressEvent(BaseModel):
    """SSE 事件体。"""

    run_id: str
    stage: str
    shot_id: str | None = None
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
