"""Analytics port.

One adapter per platform analytics source. Each observation carries the raw
platform payload untouched *and* a normalized view, so the two stay
distinguishable and normalization can be re-derived later from the raw data.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, JsonValue

from app.platforms import Platform


class NormalizedMetrics(BaseModel):
    """Comparable signals across platforms. ``None`` means the platform did not
    report the signal — never treat it as zero."""

    model_config = ConfigDict(frozen=True)

    views: int | None = None
    impressions: int | None = None
    average_watch_seconds: float | None = None
    average_watch_fraction: float | None = None
    completion_rate: float | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    follows: int | None = None


class MetricObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    platform_post_id: str
    observed_at: datetime
    raw: dict[str, JsonValue]
    normalized: NormalizedMetrics


class AnalyticsAdapter(Protocol):
    @property
    def platform(self) -> Platform: ...

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        """Current cumulative metrics for one post."""
        ...
