"""业务主表：项目、角色、角色基准图、场景、镜头。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import GateStatus, ShotStatus

if TYPE_CHECKING:
    from app.models.tracking import RenderAttempt


def _uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    """一部漫剧或一集。预算与成本的归属主体。"""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200))
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True)
    style: Mapped[str] = mapped_column(String(80), default="日系厚涂")
    # 全局画风锁定：所有镜头共用，保证画风收敛
    global_negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    budget_cents: Mapped[int] = mapped_column(Integer, default=0)
    storyboard_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    characters: Mapped[list[Character]] = relationship(back_populates="project", cascade="all, delete-orphan")
    scenes: Mapped[list[Scene]] = relationship(back_populates="project", cascade="all, delete-orphan")
    runs: Mapped[list[PipelineRun]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Character(Base):
    """角色档案 = 三级一致性锚定的第 1 级（全局特征库固化）。

    关键设计：特征存结构化字段，anchor_prompt 由程序化拼装生成并版本化，
    禁止人手写——人手写会不自觉换措辞，模型会当成不同角色。
    """

    __tablename__ = "characters"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_characters_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    # 原始自然语言设定：保留创作意图，结构化特征是模型提取后的可审计结果。
    source_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 结构化特征：只写可验证的客观特征，禁用"清秀/帅气/温柔"等主观词
    face_features: Mapped[dict] = mapped_column(JSON, default=dict)
    hair_features: Mapped[dict] = mapped_column(JSON, default=dict)
    body_features: Mapped[dict] = mapped_column(JSON, default=dict)
    outfit_features: Mapped[dict] = mapped_column(JSON, default=dict)
    style_lock: Mapped[dict] = mapped_column(JSON, default=dict)

    # 程序化拼装产物 + 版本号；质检判定时必须记录当时用的是哪一版，否则前后数据不可比
    anchor_prompt: Mapped[str] = mapped_column(Text, default="")
    anchor_version: Mapped[int] = mapped_column(Integer, default=1)

    # 人机关卡 A：角色设定必须人工确认后才能进分镜，不可跳过
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    project: Mapped[Project] = relationship(back_populates="characters")
    refs: Mapped[list[CharacterRef]] = relationship(back_populates="character", cascade="all, delete-orphan")


class CharacterRef(Base):
    """角色基准图 = 第 2 级锚定（参考图主体绑定）。

    基准图本身必须先过质检：基准图有畸形，后面全崩。
    """

    __tablename__ = "character_refs"
    __table_args__ = (UniqueConstraint("character_id", "asset_sha256", name="uq_refs_character_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    character_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="CASCADE"), index=True
    )
    # front_half / side_half / full_body / expression_*
    ref_type: Mapped[str] = mapped_column(String(40))
    asset_path: Mapped[str] = mapped_column(String(500))
    asset_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 参考图权重：强化锚定时会调高，需可追溯
    ref_weight: Mapped[float] = mapped_column(default=1.0)
    qc_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    uploaded_anchor_version: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_anchor_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_version: Mapped[int] = mapped_column(Integer, default=0)

    character: Mapped[Character] = relationship(back_populates="refs")


class Scene(Base):
    """场景。同场景镜头集中生成，第一张合格帧成为该场景的色彩/画风基准。"""

    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("project_id", "seq", name="uq_scenes_project_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    location: Mapped[str] = mapped_column(String(200), default="")
    time_of_day: Mapped[str] = mapped_column(String(40), default="day")
    mood: Mapped[str] = mapped_column(String(80), default="")
    background_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 场景色彩基准帧：第 3 级锚定（时序特征递延）的起点
    baseline_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="scenes")
    shots: Mapped[list[Shot]] = relationship(back_populates="scene", cascade="all, delete-orphan")


class Shot(Base):
    """分镜镜头 = 流水线最小工作单元。"""

    __tablename__ = "shots"
    __table_args__ = (
        UniqueConstraint("scene_id", "seq", name="uq_shots_scene_seq"),
        Index("idx_shots_status", "status"),
        CheckConstraint("max_retry >= 0", name="ck_shots_max_retry"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scene_id: Mapped[str] = mapped_column(String(36), ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)

    shot_size: Mapped[str] = mapped_column(String(20), default="中景")
    camera_move: Mapped[str | None] = mapped_column(String(40), nullable=True)
    composition: Mapped[str] = mapped_column(Text, default="")
    character_ids: Mapped[list] = mapped_column(JSON, default=list)
    action_text: Mapped[str] = mapped_column(Text, default="")
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=5000)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 第 3 级锚定：上一合格帧作为本镜头参考输入
    prev_locked_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=ShotStatus.PENDING.value)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, default=6)
    # 人工终审放行的那张帧
    locked_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    accepted_video_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 乐观锁：防并发覆盖（与农牧项目的单据版本校验同一思路）
    version: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    scene: Mapped[Scene] = relationship(back_populates="shots")
    attempts: Mapped[list[RenderAttempt]] = relationship(back_populates="shot", cascade="all, delete-orphan")


class PipelineRun(Base):
    """流水线运行实例。支持断点续跑：状态持久化，进程重启不重复扣费。"""

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("idx_runs_project_status", "project_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # LangGraph 状态快照：断点续跑的依据
    graph_state: Mapped[dict] = mapped_column(JSON, default=dict)
    current_stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 累计花费（分），从 budget_ledger 汇总而来，此处为热读冗余
    spent_cents: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="runs")
    gates: Mapped[list[ReviewGate]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ReviewGate(Base):
    """人机关卡留痕。三道关卡均不可跳过，对应"先审后播"监管要求。"""

    __tablename__ = "review_gates"
    __table_args__ = (Index("idx_gates_run", "run_id", "gate_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True
    )
    gate_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=GateStatus.PENDING.value)
    reviewer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审核时的内容快照，防事后篡改争议
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    run: Mapped[PipelineRun] = relationship(back_populates="gates")
