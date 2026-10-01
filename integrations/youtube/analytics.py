"""YouTube analytics adapter (Data API v3 + YouTube Analytics API).

Two sources are combined in one observation:
- the Data API's live counters (views, likes, comments), available at once;
- the Analytics API's watch-time metrics, which lag 48–72 hours and are
  simply absent (``None``) until YouTube has processed them.

Made-for-kids videos have comments and saves disabled, so those signals are
structurally zero or missing on YouTube; fitness gives them no weight there.
"""

import time
from datetime import UTC, date, datetime
from typing import Any

import httpx

from app.analytics.ports import AnalyticsError, MetricObservation, NormalizedMetrics
from app.platforms import Platform
from integrations.credentials import TokenStore
from integrations.http import request_json

TOKEN_URL = "https://oauth2.googleapis.com/token"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
_METRICS = (
    "views,engagedViews,estimatedMinutesWatched,averageViewDuration,"
    "averageViewPercentage,likes,shares,subscribersGained"
)


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None


class YouTubeAnalytics:
    platform = Platform.YOUTUBE_SHORTS
    name = "youtube"
    version = "1"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        tokens: TokenStore,
        client: httpx.Client | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._access: dict[str, tuple[str, float]] = {}

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        headers = {"Authorization": f"Bearer {self._access_token(platform_account_id)}"}
        videos = request_json(
            self._client,
            "GET",
            VIDEOS_URL,
            service="YouTube Data API",
            params={"part": "statistics", "id": platform_post_id},
            headers=headers,
        )
        if not videos.get("items"):
            raise AnalyticsError(f"YouTube video {platform_post_id} not found", retryable=False)
        video = videos["items"][0]
        analytics = request_json(
            self._client,
            "GET",
            REPORTS_URL,
            service="YouTube Analytics API",
            params={
                "ids": f"channel=={platform_account_id}",
                "startDate": "2020-01-01",
                "endDate": date.today().isoformat(),
                "metrics": _METRICS,
                "filters": f"video=={platform_post_id}",
            },
            headers=headers,
        )
        row: dict[str, Any] = {}
        if analytics.get("rows"):
            names = [header["name"] for header in analytics.get("columnHeaders", [])]
            row = dict(zip(names, analytics["rows"][0], strict=False))
        statistics = video.get("statistics", {})
        percentage = row.get("averageViewPercentage")
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw={"data_api": video, "analytics": analytics},
            normalized=NormalizedMetrics(
                views=_int(statistics.get("viewCount")),
                likes=_int(statistics.get("likeCount")),
                comments=_int(statistics.get("commentCount")),
                shares=_int(row.get("shares")),
                follows=_int(row.get("subscribersGained")),
                average_watch_seconds=row.get("averageViewDuration"),
                average_watch_fraction=percentage / 100 if percentage is not None else None,
            ),
        )

    def _access_token(self, channel_id: str) -> str:
        cached = self._access.get(channel_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        stored = self._tokens.get(f"youtube:{channel_id}")
        if not stored or not stored.get("refresh_token"):
            raise AnalyticsError(
                f"no YouTube authorization stored for channel {channel_id}: run "
                f"`hatch auth youtube {channel_id}`",
                retryable=False,
            )
        token = request_json(
            self._client,
            "POST",
            TOKEN_URL,
            service="Google OAuth",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": stored["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        access = str(token["access_token"])
        self._access[channel_id] = (access, time.monotonic() + int(token.get("expires_in", 0)) - 60)
        return access
