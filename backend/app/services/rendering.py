"""图像生成编排的无副作用边界。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.knowledge.anchor import AnchorInput, build_anchor_prompt
from app.models.enums import AttemptStatus, ProviderKind
from app.models.tracking import RenderAttempt
from app.providers.base import CallContext, ProviderResult, ProviderRouter


@dataclass(frozen=True)
class RenderSpec:
    project_id: str
    run_id: str
    shot_id: str
    attempt_no: int
    anchor_version: int
    model: str = settings.siliconflow_image_model
    seed: str | None = None
    attempt_id: str | None = None

    def context(self) -> CallContext:
        if self.attempt_no < 1:
            raise ValueError("attempt_no 必须从 1 开始")
        return CallContext(
            kind=ProviderKind.IMAGE,
            project_id=self.project_id,
            run_id=self.run_id,
            operation_key=f"render:{self.shot_id}:{self.attempt_no}",
            shot_id=self.shot_id,
            attempt_id=self.attempt_id,
            attempt_no=self.attempt_no,
            seed=self.seed,
        )

def _character_prompt(character: Any, *, level: str, fields: list[str]) -> str:
    prompt = str(getattr(character, "anchor_prompt", "") or "").strip()
    if prompt and level == "normal" and not fields:
        return prompt
    data = AnchorInput(
        name=str(getattr(character, "name", "角色")),
        face_features=dict(getattr(character, "face_features", {}) or {}),
        hair_features=dict(getattr(character, "hair_features", {}) or {}),
        body_features=dict(getattr(character, "body_features", {}) or {}),
        outfit_features=dict(getattr(character, "outfit_features", {}) or {}),
        style_lock=dict(getattr(character, "style_lock", {}) or {}),
    )
    return build_anchor_prompt(data, level=level, strengthen_fields=fields)

def build_image_payload(
    *,
    project: Any,
    scene: Any,
    shot: Any,
    characters: list[Any],
    reference_paths: list[str] | None = None,
    anchor_level: str = "normal",
    strengthen_fields: list[str] | None = None,
    seed: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """从已锁定的数据库快照生成稳定、可审计的供应商请求参数。"""
    fields = list(strengthen_fields or [])
    anchors = [_character_prompt(character, level=anchor_level, fields=fields) for character in characters]
    parts = [
        str(getattr(project, "style", "") or ""),
        str(getattr(scene, "location", "") or ""),
        str(getattr(scene, "time_of_day", "") or ""),
        str(getattr(scene, "mood", "") or ""),
        str(getattr(scene, "background_prompt", "") or ""),
        str(getattr(shot, "shot_size", "") or ""),
        str(getattr(shot, "composition", "") or ""),
        str(getattr(shot, "action_text", "") or ""),
        str(getattr(shot, "dialogue", "") or ""),
        *anchors,
    ]
    negative = "；".join(
        part
        for part in (
            str(getattr(project, "global_negative_prompt", "") or ""),
            str(getattr(shot, "negative_prompt", "") or ""),
        )
        if part
    )
    payload: dict[str, Any] = {
        "model": model or settings.siliconflow_image_model,
        "prompt": "；".join(part for part in parts if part),
        "negative_prompt": negative,
        "image_paths": list(reference_paths or []),
        "anchor_level": anchor_level,
        "strengthen_fields": fields,
        "n": 1,
    }
    if seed is not None:
        payload["seed"] = seed
    return payload

def prepare_attempt(*, spec: RenderSpec, payload: dict[str, Any]) -> tuple[RenderAttempt, CallContext]:
    context = spec.context()
    key, _ = context.identity(payload)
    attempt = RenderAttempt(
        id=spec.attempt_id,
        shot_id=spec.shot_id,
        attempt_no=spec.attempt_no,
        stage="image",
        provider="router",
        model=str(payload["model"]),
        seed=spec.seed,
        request_payload=payload,
        anchor_version=spec.anchor_version,
        status=AttemptStatus.PENDING.value,
        idempotency_key=key,
    )
    return attempt, context

def apply_provider_result(attempt: RenderAttempt, result: ProviderResult) -> None:
    attempt.provider = result.provider
    attempt.model = result.model
    attempt.asset_path = result.asset_path
    attempt.asset_sha256 = result.asset_sha256
    attempt.cost_cents = result.cost_cents
    attempt.latency_ms = result.latency_ms
    attempt.error_code = result.error_code
    attempt.status = AttemptStatus.SUCCEEDED.value if result.success else AttemptStatus.FAILED.value
    attempt.finished_at = datetime.now(UTC).replace(tzinfo=None)


async def render_image(
    router: ProviderRouter, *, spec: RenderSpec, payload: dict[str, Any]
) -> ProviderResult:
    _, context = prepare_attempt(spec=spec, payload=payload)
    return await router.generate(ProviderKind.IMAGE, payload, context)
