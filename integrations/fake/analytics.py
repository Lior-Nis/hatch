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
    name = "fake"
    version = "fake-1"

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
            normalized=NormalizedMetrics.model_validate(
                {
                    # Raw keys named like normalized signals pass straight through.
                    **{k: v for k, v in raw.items() if k in NormalizedMetrics.model_fields},
                    **({"views": _as_int(raw["play_count"])} if "play_count" in raw else {}),
                    **(
                        {"completion_rate": _as_float(raw["full_video_watched_rate"])}
                        if "full_video_watched_rate" in raw
                        else {}
                    ),
                }
            ),
        )
