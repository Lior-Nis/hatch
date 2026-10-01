"""Fake analytics adapter serving canned raw payloads."""

from datetime import UTC, datetime

from pydantic import JsonValue

from app.analytics.ports import MetricObservation, NormalizedMetrics
from app.platforms import Platform


def _as_int(value: JsonValue) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _as_float(value: JsonValue) -> float | None:
    return float(value) if isinstance(value, int | float) else None


class FakeAnalyticsAdapter:
    def __init__(self, *, platform: Platform, payloads: dict[str, dict[str, JsonValue]]) -> None:
        self.platform = platform
        self._payloads = payloads

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        raw = self._payloads[platform_post_id]
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw=raw,
            normalized=NormalizedMetrics(
                views=_as_int(raw.get("play_count")),
                completion_rate=_as_float(raw.get("full_video_watched_rate")),
            ),
        )
