"""项目与准备运行，尚未接通的生成流程不投递任务。"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import NotFoundError
from app.core.response import ok
from app.models.domain import PipelineRun, Project, ReviewGate
from app.models.tracking import BudgetLedger
from app.schemas import GateOut, ProjectCreate, ProjectOut, RunOut, StoryboardDecision
from app.services.catalog import project_or_404
from app.services.preparation import advance_preparation, board_snapshot, decide_storyboard, get_run

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


async def publish_run(db: AsyncSession, run: PipelineRun) -> None:
    import logging

    from app.core.redis_client import publish_progress

    # MySQL 是恢复依据。提交成功后再广播；Redis 短暂离线不回滚已保存的人工决定。
    data = RunOut.model_validate(run).model_dump(mode="json")
    await db.commit()
    try:
        await publish_progress(run.id, run.current_stage or "preparation", data)
    except Exception:
        logging.getLogger(__name__).warning("进度广播暂不可用 run_id=%s", run.id)


@router.get("/{project_id}/storyboard", summary="读取准备清单与分镜版本")
async def get_storyboard(project_id: str, run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    project = await project_or_404(db, project_id, lock=True)
    run = await get_run(db, project, run_id)
    return ok(await board_snapshot(db, project, run.id))


@router.post("/{project_id}/storyboard/review", summary="B 关卡：审核当前分镜版本")
async def review_storyboard(
    project_id: str, body: StoryboardDecision, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    project = await project_or_404(db, project_id, lock=True)
    gate = await decide_storyboard(db, project, body)
    result = GateOut.model_validate(gate).model_dump(mode="json")
    run = await get_run(db, project, body.run_id)
    await publish_run(db, run)
    return ok(result)


@router.post("/{project_id}/runs/{run_id}/resume", summary="检查准备项并恢复至下一待办关卡")
async def resume_run(project_id: str, run_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    project = await project_or_404(db, project_id, lock=True)
    run = await get_run(db, project, run_id)
    await advance_preparation(db, project, run)
    result = RunOut.model_validate(run).model_dump(mode="json")
    await publish_run(db, run)
    return ok(result)


@router.post("/{project_id}/runs", summary="继续项目的准备运行")
async def start_run(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    project = await project_or_404(db, project_id, lock=True)
    run = await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.project_id == project.id)
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id)
        .with_for_update()
        .limit(1)
    )
    if run is None:
        raise NotFoundError("项目没有可恢复的准备运行")
    await get_run(db, project, run.id)
    await advance_preparation(db, project, run)
    result = RunOut.model_validate(run).model_dump(mode="json")
    await publish_run(db, run)
    return ok(result)


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
