"""项目路由：CRUD 与流水线触发。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.response import ok
from app.schemas import ProjectCreate, ProjectOut, RunOut

router = APIRouter()


@router.post("", summary="创建项目")
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """创建项目并初始化三级预算账本（project / kind / shot）。

    TODO(impl):
      1. body.budget_cents 为 None 时取 settings.budget_total_cents
      2. 写入 projects 表
      3. 调 BudgetService.init_ledger(project_id) 建预算行（见 services/budget_service.py）
      4. 返回 ProjectOut
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("", summary="项目列表")
async def list_projects(
    page: int = 1, page_size: int = 20, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """TODO(impl): 分页查询，按 updated_at 倒序。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{project_id}", summary="项目详情", response_model=None)
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 查不到抛 NotFoundError。返回 ProjectOut。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{project_id}/runs", summary="启动流水线运行")
async def start_run(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """创建 PipelineRun 并投递到 ARQ 队列。

    TODO(impl):
      1. 校验项目存在且已有确认的角色（人机关卡 A 前置条件）
      2. 建 pipeline_runs 行，status=pending
      3. 投递 ARQ 任务 enqueue_pipeline_run(run_id)
      4. 返回 RunOut

    注意：不要在这里同步执行流水线。分钟级任务必须走队列，
    否则 HTTP 请求会一直挂着，且进程重启会丢任务。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{project_id}/runs", summary="运行历史")
async def list_runs(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 返回该项目的 PipelineRun 列表，按 created_at 倒序。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")
