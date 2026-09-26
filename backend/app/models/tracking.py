"""记账与质检表：抽卡尝试、质检报告、黄金测试集、API 调用日志、预算账本、成片导出。

这几张表是简历上所有量化指标的唯一数据来源，第一周必须打通。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import AttemptStatus, GoldenLabel, QCVerdict

if TYPE_CHECKING:
    from app.models.domain import Shot


def _uuid() -> str:
    return str(uuid.uuid4())


class RenderAttempt(Base):
    """每次抽卡尝试。成本与成功率的原始数据。

    幂等键 = sha256(shot_id|attempt_no|model|seed)，配唯一索引：
    即使 Worker 崩溃重启，也不会对同一次抽卡重复调用 API 扣费。
    """

    __tablename__ = "render_attempts"
    __table_args__ = (
        UniqueConstraint("shot_id", "attempt_no", name="uq_attempts_shot_no"),
        UniqueConstraint("idempotency_key", name="uq_attempts_idempotency"),
        Index("idx_attempts_status", "status"),
        Index("idx_attempts_shot_stage", "shot_id", "stage"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    shot_id: Mapped[str] = mapped_column(String(36), ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(20), default="image")

    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    seed: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 实际发送的完整参数：Trace 系统的基础，能回答"这张废片当时用的什么 Prompt/种子/模型"
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # 判定当时用的角色锚定版本；不记版本则前后数据不可比，优化效果无法归因
    anchor_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    asset_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    asset_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=AttemptStatus.PENDING.value)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    shot: Mapped[Shot] = relationship(back_populates="attempts")
    qc_reports: Mapped[list[QCReport]] = relationship(back_populates="attempt", cascade="all, delete-orphan")


class QCReport(Base):
    """质检判定报告。结构化、可复现、可回归——这是它能被黄金测试集校准的前提。"""

    __tablename__ = "qc_reports"
    __table_args__ = (
        Index("idx_qc_attempt", "attempt_id"),
        Index("idx_qc_verdict", "verdict"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("render_attempts.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(80))
    verdict: Mapped[str] = mapped_column(String(20), default=QCVerdict.PASS.value)
    # {facial_deformity, identity_drift, color_discontinuity, composition, compliance}
    # 每项含 score / ok / note；identity_drift 另含 drifted_fields
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    suggestion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict] = mapped_column(JSON, default=dict)

    # 人工复核：黄金测试集标注与质检 Agent 校准的数据来源
    human_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    human_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    attempt: Mapped[RenderAttempt] = relationship(back_populates="qc_reports")


class QCGoldenSet(Base):
    """黄金测试集（人工标注）。

    用 asset_key（内容 sha256）而非自增 ID 做唯一键：
    同一张图不会被重复标注，且测试集可跨环境复现。

    素材来源：正式抽卡的副产物，不额外花钱。
    """

    __tablename__ = "qc_golden_set"
    __table_args__ = (
        UniqueConstraint("asset_key", name="uq_golden_asset"),
        Index("idx_golden_label", "label"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    asset_key: Mapped[str] = mapped_column(String(64))
    asset_path: Mapped[str] = mapped_column(String(500))
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    label: Mapped[str] = mapped_column(String(30), default=GoldenLabel.PASS.value)
    label_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    labeled_by: Mapped[str] = mapped_column(String(80), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProviderRequest(Base):
    """一次逻辑请求。重放跨供应商返回同一结果，未知结果禁止重新提交。"""

    __tablename__ = "provider_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_provider_request_key"),
        Index("idx_requests_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipeline_runs.id", ondelete="CASCADE"))
    operation_key: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    owner_token: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="calling")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ApiCallLog(Base):
    """逐次 API 调用记账。失败的调用也要记。

    失败也记的两个理由：
    1. 失败同样消耗限流配额（多数供应商按成功计费，但并发额度是共享的）
    2. 供应商成功率对比是路由策略的依据——限流严重时实际可用性低于单价优势
    """

    __tablename__ = "api_call_logs"
    __table_args__ = (
        Index("idx_calls_project_time", "project_id", "created_at"),
        Index("idx_calls_provider", "provider", "model", "success"),
        UniqueConstraint("request_id", "call_no", name="uq_calls_request_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("provider_requests.id", ondelete="CASCADE", name="fk_calls_request"),
        nullable=True,
    )
    call_no: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(20), default="calling", server_default="calling")
    provider_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quoted_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reported_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    shot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # image/video/vision/llm
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(80))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class BudgetLedger(Base):
    """三级预算在持有项目行锁的同一事务内预留/结算，禁止无锁先查后写。"""

    __tablename__ = "budget_ledger"
    __table_args__ = (
        UniqueConstraint("project_id", "scope", "scope_key", name="uq_budget_scope"),
        CheckConstraint(
            "spent_cents >= 0 AND reserved_cents >= 0 AND spent_cents + reserved_cents <= budget_cents",
            name="ck_budget_available",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(20))  # project/shot/kind
    scope_key: Mapped[str] = mapped_column(String(120))  # 镜头 ID 或 image/video/vision/llm
    budget_cents: Mapped[int] = mapped_column(Integer)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Export(Base):
    """成片导出记录。"""

    __tablename__ = "exports"
    __table_args__ = (Index("idx_exports_project", "project_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 本次导出的成本与成功率快照，直接用于简历数据
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
