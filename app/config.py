"""Runtime configuration.

Every setting is read from an environment variable prefixed with ``HATCH_``
(optionally via a local ``.env`` file). Budget ceilings are part of the product
contract: only the human operator may raise them, by changing the environment.
No code path may write to them.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: str = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    database_url: str = "postgresql+psycopg://hatch:hatch@localhost:54329/hatch"

    # Generated media. "local" keeps files under asset_dir; "s3" uses any
    # S3-compatible object store (Cloudflare R2, AWS S3) and asset_dir as a
    # download cache.
    asset_store: Literal["local", "s3"] = "local"
    asset_dir: Path = Path("var/assets")
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    # Media generation. "fake" renders free synthetic test videos locally.
    media_provider: Literal["higgsfield", "fake"] = "higgsfield"
    higgsfield_api_key: SecretStr | None = None
    higgsfield_api_secret: SecretStr | None = None
    generation_poll_interval_seconds: float = 5.0
    generation_timeout_seconds: float = 900.0

    # Language model for creative agents and content QA (Claude API).
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"

    # Name recorded on human review decisions. Defaults to the OS user.
    reviewer: str | None = None

    # Budget contract (USD). Defaults are the PRD's initial production limits.
    budget_target_per_video_usd: Decimal = Decimal("0.50")
    budget_max_per_video_usd: Decimal = Decimal("1.50")
    # The PRD requires a per-generation ceiling but names no figure. Half the
    # per-video maximum leaves room for one retry inside the video budget.
    budget_max_per_generation_usd: Decimal = Decimal("0.75")
    budget_daily_usd: Decimal = Decimal("15.00")
    budget_monthly_usd: Decimal = Decimal("500.00")


@lru_cache
def get_settings() -> Settings:
    return Settings()
