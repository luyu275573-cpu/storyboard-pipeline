"""项目与准备运行，尚未接通的生成流程不投递任务。"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AppError
from app.core.response import ok
from app.models.domain import PipelineRun, Project, ReviewGate
from app.models.tracking import BudgetLedger
from app.schemas import GateOut, ProjectCreate, ProjectOut, RunOut
from app.services.catalog import project_or_404

router = APIRouter()


@router.post("", summary="创建项目及准备运行", status_code=201)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    values = body.model_dump(exclude={"budget_cents"})
    budget = settings.budget_total_cents if body.budget_cents is None else body.budget_cents
    project = Project(**values, budget_cents=budget)
    db.add(project)
    await db.flush()
    for scope, key in [("project", "total"), *(("kind", k) for k in ("image", "video", "vision", "llm"))]:
        db.add(
            BudgetLedger(
                project_id=project.id, scope=scope, scope_key=key, budget_cents=budget, spent_cents=0
            )
        )
    db.add(
        PipelineRun(
            project_id=project.id,
            status="waiting_gate",
            current_stage="character",
            graph_state={"mode": "preparation"},
        )
    )
    await db.flush()
    # MySQL 无 INSERT RETURNING，显式取回服务端生成的时间字段。
    await db.refresh(project)
    return ok(ProjectOut.model_validate(project).model_dump(mode="json"))


@router.get("", summary="项目列表")
async def list_projects(
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=100),
    q: str = Query(default="", max_length=200),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    filters = [Project.title.contains(q.strip(), autoescape=True)] if q.strip() else []
    total = await db.scalar(select(func.count()).select_from(Project).where(*filters))
    rows = await db.scalars(
        select(Project)
        .where(*filters)
        .order_by(Project.created_at.desc(), Project.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ok(
        {
            "items": [ProjectOut.model_validate(p).model_dump(mode="json") for p in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/{project_id}", summary="项目详情")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return ok(ProjectOut.model_validate(await project_or_404(db, project_id)).model_dump(mode="json"))


@router.post("/{project_id}/runs", summary="启动生成（后续批次）")
async def start_run(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await project_or_404(db, project_id)
    raise AppError("生成链路尚未启用，请先完成角色建档与确认")


@router.get("/{project_id}/runs", summary="运行历史")
async def list_runs(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await project_or_404(db, project_id)
    rows = await db.scalars(
        select(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id)
    )
    return ok([RunOut.model_validate(row).model_dump(mode="json") for row in rows])


@router.get("/{project_id}/gates", summary="人工审核记录")
async def list_gates(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await project_or_404(db, project_id)
    rows = await db.scalars(
        select(ReviewGate)
        .join(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(ReviewGate.created_at.desc(), ReviewGate.id)
    )
    return ok([GateOut.model_validate(row).model_dump(mode="json") for row in rows])
