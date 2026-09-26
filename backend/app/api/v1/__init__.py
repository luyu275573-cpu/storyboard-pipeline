"""v1 路由聚合。

⚠️ main.py 通过 `from app.api.v1 import api_router` 导入，
此文件缺失会导致应用无法启动。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import budget, characters, health, projects, qc, scenes, shots, stream

api_router = APIRouter()

api_router.include_router(health.router, tags=["健康检查"])
api_router.include_router(projects.router, prefix="/projects", tags=["项目"])
api_router.include_router(characters.router, prefix="/characters", tags=["角色与锚定"])
api_router.include_router(scenes.router, prefix="/scenes", tags=["场景"])
api_router.include_router(shots.router, prefix="/shots", tags=["分镜镜头"])
api_router.include_router(qc.router, prefix="/qc", tags=["质检与评估"])
api_router.include_router(budget.router, prefix="/budget", tags=["预算与成本"])
api_router.include_router(stream.router, prefix="/stream", tags=["实时进度 SSE"])

__all__ = ["api_router"]
