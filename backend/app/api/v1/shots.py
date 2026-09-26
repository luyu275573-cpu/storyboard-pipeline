"""分镜镜头路由：抽卡触发、质检联动、人机关卡、视频与合成。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas import AttemptOut, GateDecision, RenderRequest, ShotCreate, ShotOut

router = APIRouter()


@router.post("", summary="创建分镜镜头")
async def create_shot(body: ShotCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl):
      1. 校验 scene_id 存在、seq 在同场景内唯一（撞唯一索引则抛 ConflictError）
      2. max_retry 取 settings.shot_max_retry
      3. 若同场景已有合格帧，自动写入 prev_locked_attempt_id（第 3 级锚定：时序递延）
      4. 返回 ShotOut
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("", summary="镜头列表")
async def list_shots(
    scene_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """TODO(impl): 按 scene_id / project_id / status 过滤，按 (scene.seq, shot.seq) 排序。

    注意排序不是按镜头序号，而是按场景分组——同场景集中生成是色彩一致性的前提。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{shot_id}", summary="镜头详情")
async def get_shot(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 含 attempts 与最新 qc_report。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{shot_id}/render", summary="触发抽卡")
async def render_shot(
    shot_id: str, body: RenderRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """核心接口：触发图像或视频抽卡。

    TODO(impl):
      1. 校验镜头所属角色已 confirmed（人机关卡 A），否则抛 GatePendingError
      2. 校验预算：CostService.precheck(project_id, estimated_cents)
      3. 校验 retry_count < max_retry，超限抛 MaxRetryError（挂起转人工，不静默失败）
      4. 镜头级互斥锁 lock:shot:{shot_id}，防并发重复抽卡
      5. 组装 anchor_prompt（按 body.anchor_level / strengthen_fields）
      6. 投递 ARQ 任务 enqueue_render(shot_id, n, stage, anchor_level, strengthen_fields)
      7. 返回 {shot_id, queued: n, attempt_nos: [...]}

    ⚠️ 不要在此同步调用 Provider。图像生成秒级、视频分钟级，
       同步等待会占满 worker 并触发 HTTP 超时。必须走队列。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{shot_id}/attempts", summary="抽卡历史")
async def list_attempts(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 返回 AttemptOut 列表，按 attempt_no 升序。

    这是 Trace 的入口：每条 attempt 都能追到 request_payload、anchor_version、
    cost_cents、qc_report，回答"这张废片当时用的什么参数"。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{shot_id}/gate/compliance", summary="人机关卡 C：先审后播合规终审")
async def compliance_gate(
    shot_id: str, body: GateDecision, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """强制人工关卡。质检 Agent 只给建议，放行权在人。

    TODO(impl):
      1. 校验该镜头有 verdict=pass 的质检报告，否则抛 GatePendingError
      2. approved → 写 locked_attempt_id，status=review→video，version+=1（乐观锁）
         rejected → status=suspended，记录 note
      3. 写 review_gates(gate_type='compliance', snapshot=当前帧与判定快照)
      4. 被 QC 判 blocked（合规拦截）的镜头禁止在此放行，必须重新生成
         —— 对应 2025.9《管理提示（动画微短剧管理）》的"先审后播"要求
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{shot_id}/video", summary="触发视频生成")
async def generate_video(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl):
      1. 前置：必须已通过合规关卡（locked_attempt_id 非空），否则抛 GatePendingError
      2. stage='video'，走 video_provider_chain（可灵优先，单价低）
      3. 投递 ARQ 任务，分层超时：单次请求 < 任务总超时 < 前端等待
      4. 预算不足时降级：降分辨率 / 换便宜供应商 / 挂起，不直接失败
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/synthesize", summary="FFmpeg 时间轴合成")
async def synthesize(project_id: str, run_id: str | None = None, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl):
      1. 取该场景/项目所有 locked_attempt_id 的视频片段，按 (scene.seq, shot.seq) 排序
      2. 生成 FFmpeg concat demuxer 文件清单 → 拼接 → 转场 → 导出到 storage/exports
      3. 写 exports 行，metrics 存本次成本与成功率快照（直接用于简历数据）
      4. 缺片段的镜头要显式报错列出，不能静默跳过

    注意：FFmpeg 是子进程调用，必须用 asyncio.create_subprocess_exec，
    不能用 subprocess.run（会阻塞事件循环）。设置超时与取消令牌。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")
