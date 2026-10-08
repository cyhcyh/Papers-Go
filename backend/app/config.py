from pathlib import Path
from functools import lru_cache
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    data_dir: Path = Path('data')
    frontend_dir: Path = Path('frontend/dist')
    tz: str = 'Asia/Shanghai'
    jwt_secret: str = ''
    require_invite_code: bool = False
    access_minutes: int = 30
    refresh_days: int = 30
    llm_base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    llm_api_key: str = ''
    llm_model_chat: str = 'qwen-plus'
    llm_model_precise: str = 'qwen-max'
    llm_model_fast: str = 'qwen-turbo'
    llm_timeout: float = 120
    reading_context_tokens: int = Field(default=0, ge=0, le=2000000)
    reading_cloud_concurrency: int = Field(default=2, ge=1, le=8)
    reading_local_concurrency: int = Field(default=1, ge=1, le=4)
    trend_input_tokens: int = Field(default=8000, ge=4000, le=32000)
    classify_cloud_concurrency: int = Field(default=4, ge=1, le=16)
    brief_cloud_concurrency: int = Field(default=4, ge=1, le=8)
    quality_cloud_concurrency: int = Field(default=2, ge=1, le=8)
    embedding_cloud_concurrency: int = Field(default=4, ge=1, le=16)
    embedding_local_concurrency: int = Field(default=1, ge=1, le=4)
    model_key_file: Path = Path('.secrets/model-keys.key')
    model_encryption_key: str = ''
    ollama_base_url: str = 'http://localhost:11434'
    ollama_model: str = 'qwen3:4b'
    embedding_model: str = 'bge-m3'
    embedding_dim: int = 1024
    github_token: str = ''
    openalex_api_key: str = ''
    author_impact_batch_size: int = Field(default=100, ge=1, le=1000)
    author_impact_seconds: int = Field(default=120, ge=10, le=600)
    telegram_bot_token: str = ''
    arxiv_categories: str = 'cs.AI,cs.LG,cs.CL,cs.CV,cs.MA,cs.NE,cs.RO,stat.ML,math.CO'
    conference_sources: str = 'ICML,AAAI,NeurIPS,ICLR'
    fetch_limit: int = 400  # First-start trial only; daily synchronization has no quantity cap.
    scheduler_enabled: bool = True
    bootstrap_enabled: bool = True
    pipeline_mode: str = 'process'  # inline is reserved for tests; external uses a separate Docker worker.
    recommendation_candidates: int = Field(default=1000, ge=100, le=5000)
    recommendation_cache_seconds: int = Field(default=120, ge=0, le=600)
    # File mapping range, not a reserved RAM allocation or a physical memory cap.
    sqlite_mmap_mb: int = Field(default=1024, ge=0, le=1024)
    vector_cache_mb: int = Field(default=256, ge=16, le=512)
    background_slow_request_ms: int = Field(default=800, ge=100, le=10000)
    chat_concurrency: int = Field(default=8, ge=1, le=100)
    interest_weight: float = .665
    quality_weight: float = .12
    novelty_weight: float = .065
    guest_quality_weight: float = .63
    guest_recency_weight: float = .35
    author_weight: float = Field(default=.15, ge=0, le=1, allow_inf_nan=False)
    guest_author_weight: float = Field(default=.02, ge=0, le=1, allow_inf_nan=False)
    collision_threshold: float = .88
    alert_batch_size: int = Field(default=500, ge=1, le=500)
    alert_seconds: int = Field(default=30, ge=1, le=120)


@lru_cache
def settings() -> Settings:
    return Settings()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(ZoneInfo(settings().tz)).date().isoformat()
