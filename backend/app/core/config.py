"""应用配置：全部来自环境变量，禁止在代码里硬编码密钥或单价。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = 8100
    app_debug: bool = True
    app_cors_origins: str = "http://127.0.0.1:5173"

    # ---------- 数据库 ----------
    database_url: str = "mysql+asyncmy://sbp:sbp_password@127.0.0.1:3306/storyboard_pipeline"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ---------- Redis ----------
    redis_url: str = "redis://127.0.0.1:6379/3"
    arq_queue_name: str = "sbp_tasks"

    # ---------- 预算（单位：分）----------
    budget_total_cents: int = 20000
    budget_per_shot_cents: int = 300
    budget_warn_ratio: float = 0.7
    budget_degrade_ratio: float = 0.9
    shot_max_retry: int = 6

    # ---------- 模型供应商 ----------
    volc_access_key: str = ""
    volc_secret_key: str = ""
    volc_image_model: str = "jimeng-3.0"
    volc_video_model: str = "jimeng-video-3.0-720p"

    dashscope_api_key: str = ""
    dashscope_vision_model: str = "qwen-vl-max"
    dashscope_llm_model: str = "qwen-plus"

    vidu_api_key: str = ""
    vidu_image_model: str = "viduq2-fast_reference2image"
    vidu_video_model: str = "viduq2-pro_img2video"

    kling_access_key: str = ""
    kling_secret_key: str = ""
    kling_video_model: str = "kling-v3-std"

    # 硅基流动：Provider 接入后启用；当前仅作为配置模板
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_image_model: str = "Qwen/Qwen-Image-Edit-2509"
    siliconflow_vision_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    siliconflow_llm_model: str = "Qwen/Qwen3-32B"
    siliconflow_video_model: str = "Wan-AI/Wan2.2-I2V-A14B"
    siliconflow_video_price_cents: int = 200

    # ---------- Provider 行为 ----------
    provider_max_concurrency: int = 1
    provider_timeout_s: int = 60
    video_timeout_s: int = 600
    provider_max_backoff_s: int = 30

    # ---------- 流水线 ----------
    render_n_per_shot: int = 2
    qc_parse_max_retry: int = 2
    image_provider_chain: str = "siliconflow,jimeng,vidu"
    video_provider_chain: str = "siliconflow"
    vision_provider_chain: str = "siliconflow,dashscope"
    llm_provider_chain: str = "siliconflow,dashscope"

    # ---------- 存储与工具 ----------
    storage_root: str = "../storage"
    max_upload_mb: int = 20
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # ---------- 日志 ----------
    log_level: str = "INFO"
    log_dir: str = "../logs"

    @field_validator("database_url")
    @classmethod
    def _must_be_async_driver(cls, v: str) -> str:
        """同步驱动会卡死事件循环，启动即失败而不是运行时才发现。"""
        if "+pymysql" in v or v.startswith("mysql://"):
            msg = (
                "DATABASE_URL 必须使用异步驱动（mysql+asyncmy://）。"
                "同步 PyMySQL 会阻塞事件循环，导致并发归零。"
            )
            raise ValueError(msg)
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]

    @property
    def image_chain(self) -> list[str]:
        return [p.strip() for p in self.image_provider_chain.split(",") if p.strip()]

    @property
    def video_chain(self) -> list[str]:
        return [p.strip() for p in self.video_provider_chain.split(",") if p.strip()]

    @property
    def vision_chain(self) -> list[str]:
        return [p.strip() for p in self.vision_provider_chain.split(",") if p.strip()]

    @property
    def llm_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_chain.split(",") if p.strip()]

    @property
    def storage_path(self) -> Path:
        p = Path(self.storage_root)
        return p if p.is_absolute() else (BACKEND_ROOT / p).resolve()

    @property
    def log_path(self) -> Path:
        p = Path(self.log_dir)
        return p if p.is_absolute() else (BACKEND_ROOT / p).resolve()

    def has_image_credentials(self) -> bool:
        return bool(self.volc_access_key or self.vidu_api_key or self.siliconflow_api_key)

    def has_vision_credentials(self) -> bool:
        return bool(self.dashscope_api_key or self.siliconflow_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 预算熔断比例必须是递增的合法区间，配错会导致熔断永不触发或立即触发
assert 0 < settings.budget_warn_ratio < settings.budget_degrade_ratio <= 1.0, (  # noqa: S101
    "预算比例配置非法：要求 0 < warn < degrade <= 1.0"
)
