"""质检与评估路由：判定、人工复核、黄金测试集、混淆矩阵。

这是项目的核心差异化能力所在，也是简历量化指标的数据源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.response import ok
from app.models.tracking import QCReport
from app.schemas import GoldenLabelCreate, QCInspectRequest, QCReportOut, QCReviewRequest

router = APIRouter()


@router.post("/inspect", summary="对关键帧执行质检")
async def inspect(body: QCInspectRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """执行五维判定并落库。

    TODO(impl):
      1. 读 attempt 及其 shot / scene / characters
      2. image_paths = [基准帧?, 待判定帧]
         基准帧来源优先级：body.baseline_attempt_id → scene.baseline_attempt_id → shot.prev_locked_attempt_id
         有基准帧才置 has_baseline_frame=True（色彩断层维度靠视觉比对，纯文字判不出）
      3. 调 QCAgent.inspect(...)，注入 anchor_prompt 与 anchor_version
      4. 写 qc_reports（dimensions/severity/suggestion/confidence/reasoning/raw_response）
      5. 按 verdict 更新 shot.status，并返回下一步动作建议：
         pass          → status=review，等人机关卡 C
         repairable    → 按 suggestion 触发对应重抽（reseed/redraw/strengthen_anchor）
         reject        → retry_count += 1；超 max_retry 则 status=suspended
         blocked       → status=suspended，禁止自动重试（合规拦截）
      6. 若该帧 pass 且场景尚无基准帧，把它设为 scene.baseline_attempt_id
         —— 第 3 级锚定的起点就是这么建立的

    ⚠️ 质检调用要走免费 Token 额度的视觉模型。质检是调用量最大的一环
       （每张图至少判一次，重抽还要再判），按量付费会吃光预算。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/reports/{attempt_id}", summary="查质检报告")
async def get_report(attempt_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    report = await db.scalar(
        select(QCReport)
        .where(QCReport.attempt_id == attempt_id)
        .order_by(QCReport.created_at.desc(), QCReport.id.desc())
        .limit(1)
    )
    if report is None:
        raise NotFoundError("该关键帧尚无质检报告")
    return ok(QCReportOut.model_validate(report).model_dump(mode="json"))


@router.post("/reports/{report_id}/review", summary="人工复核质检结论")
async def review_report(
    report_id: str, body: QCReviewRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """写入 human_verdict，这是算混淆矩阵的前提。

    TODO(impl):
      1. human_verdict / human_note / reviewed_at 落库
      2. 不覆盖机器判定结果 —— 两者都要留，才能算出 FP/FN

    没有人工复核，"质检准确率 X%"就是自说自话。
    """
    if body.human_verdict not in {"pass", "reject"}:
        raise AppError("人工质检结论只能是 pass 或 reject")
    report = await db.scalar(select(QCReport).where(QCReport.id == report_id).with_for_update())
    if report is None:
        raise NotFoundError("质检报告不存在")
    report.human_verdict = body.human_verdict
    report.human_note = body.human_note
    report.reviewed_at = datetime.now(UTC).replace(tzinfo=None)
    await db.flush()
    return ok(QCReportOut.model_validate(report).model_dump(mode="json"))


# ==================== 黄金测试集 ====================


@router.post("/golden", summary="新增黄金测试集标注")
async def add_golden_label(body: GoldenLabelCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl):
      1. asset_key 是内容 sha256，撞唯一索引说明已标注过 → 抛 ConflictError
         （用内容指纹而非自增 ID，保证同一张图不会重复标注、且测试集可跨环境复现）
      2. 返回新建记录

    素材来源：正式抽卡的副产物，不额外花钱。
    盲抽基线那 100 张里有大量不合格样本，正好是标注需要的。
    标注分布建议：合格 30% / 五官畸形 20% / 人设漂移 25% / 色彩断层 10% / 构图 10% / 合规 5%
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/golden", summary="黄金测试集列表")
async def list_golden(label: str | None = None, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 支持按 label 过滤，返回各类别计数分布（检查标注是否均衡）。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/golden/stats", summary="黄金测试集分布统计")
async def golden_stats(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 各 label 计数与占比。

    用途：检查标注分布是否均衡。人设漂移类样本不足会导致该维度指标不可信。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


# ==================== 校准指标 ====================


@router.get("/evaluate", summary="质检 Agent 校准指标（混淆矩阵）")
async def evaluate(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """在黄金测试集上跑质检 Agent，算混淆矩阵与业务指标。

    TODO(impl):
      1. 取 qc_golden_set 全部样本（可加 label 过滤做分层评估）
      2. 对每张跑 QCAgent（或用已存的 qc_reports + human_verdict 直接算，省钱）
         —— 优先用已有人工复核数据计算，不重复调 API
      3. 统计：
            TP = 机器 pass & 人工 pass      TN = 机器 reject & 人工 reject
            FP = 机器 reject & 人工 pass    FN = 机器 pass & 人工 reject
      4. accuracy = (TP+TN)/total
         recall = TP/(TP+FN)                        ← 合格帧识别率
         false_positive_rate = FP/(FP+TN)           ← 误杀率，直接对应浪费的重抽成本
      5. cost_fn_cents = FN * 140（漏放一张废片到视频层的损失）
         cost_fp_cents = FP * 25（误杀一张的重抽损失）
      6. dimension_breakdown：各维度不合格计数 → 错误分类体系，反向驱动优化
         人设漂移占比最高 → 加强第 1/2 级锚定
         五官畸形占比最高 → 换供应商或调 CFG
         色彩断层占比最高 → 检查同场景集中生成策略

    返回 ConfusionMatrix。这是简历上"召回率 X%、误杀率 Y%"的数据源。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/first-pass-rate", summary="一次过合格率（含盲抽基线对照）")
async def first_pass_rate(
    project_id: str, baseline: bool = False, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """TODO(impl):
      1. first_pass = attempt_no=1 且 qc verdict=pass 的镜头数 / 总镜头数
      2. baseline=True 时只统计"无锚定无质检"的盲抽批次（需在 attempt 上打标区分）
      3. 返回 {rate, sample_size, is_baseline}

    ⚠️ 没有对照组，"提升 X%"就是空话。行业公开数据是 15%，
       但你的画风、角色、Prompt 风格基线可能完全不同，必须自己测。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")
