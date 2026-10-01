"""Runtime configuration.

Every setting is read from an environment variable prefixed with ``HATCH_``
(optionally via a local ``.env`` file). Budget ceilings are part of the product
contract: only the human operator may raise them, by changing the environment.
No code path may write to them.
"""

from decimal import Decimal
from functools import lru_cache

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

    # Budget contract (USD). Defaults are the PRD's initial production limits.
    budget_target_per_video_usd: Decimal = Decimal("0.50")
    budget_max_per_video_usd: Decimal = Decimal("1.50")
    budget_daily_usd: Decimal = Decimal("15.00")
    budget_monthly_usd: Decimal = Decimal("500.00")


@lru_cache
def get_settings() -> Settings:
    return Settings()
