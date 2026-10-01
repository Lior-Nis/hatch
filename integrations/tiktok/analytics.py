"""TikTok analytics adapters.

Two official APIs, two adapters:

- ``TikTokDisplayAnalytics`` (developers.tiktok.com, usable in sandbox for
  your own accounts): public counters only — views, likes, comments, shares.
- ``TikTokBusinessAnalytics`` (API for Business, needs approval): adds reach,
  average watch time, completion rate, favourites and new followers. It is the
  only official source of TikTok watch-quality signals.

Use the Business adapter whenever its access has been granted.
"""

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.analytics.ports import AnalyticsError, MetricObservation, NormalizedMetrics
from app.platforms import Platform
from integrations.credentials import TokenStore
from integrations.http import request_json

DISPLAY_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
DISPLAY_QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"
BUSINESS_VIDEO_URL = "https://business-api.tiktok.com/open_api/v1.3/business/video/list/"
_DISPLAY_FIELDS = "id,view_count,like_count,comment_count,share_count,duration,create_time"
_BUSINESS_FIELDS = (
    "item_id", "video_views", "likes", "comments", "shares", "favorites", "reach",
    "video_duration", "total_time_watched", "average_time_watched", "full_video_watched_rate",
    "new_followers",
)  # fmt: skip


class TikTokDisplayAnalytics:
    platform = Platform.TIKTOK
    name = "tiktok_display"
    version = "1"

    def __init__(
        self,
        *,
        client_key: str,
        client_secret: str,
        tokens: TokenStore,
        client: httpx.Client | None = None,
    ) -> None:
        self._client_key = client_key
        self._client_secret = client_secret
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._access: dict[str, tuple[str, float]] = {}

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        data = request_json(
            self._client,
            "POST",
            DISPLAY_QUERY_URL,
            service="TikTok Display API",
            params={"fields": _DISPLAY_FIELDS},
            json={"filters": {"video_ids": [platform_post_id]}},
            headers={"Authorization": f"Bearer {self._access_token(platform_account_id)}"},
        )
        error = data.get("error") or {}
        if error.get("code") not in (None, "ok"):
            raise AnalyticsError(f"TikTok Display API: {error.get('message') or error['code']}")
        videos = (data.get("data") or {}).get("videos") or []
        if not videos:
            raise AnalyticsError(f"TikTok video {platform_post_id} not found", retryable=False)
        video = videos[0]
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw=video,
            normalized=NormalizedMetrics(
                views=video.get("view_count"),
                likes=video.get("like_count"),
                comments=video.get("comment_count"),
                shares=video.get("share_count"),
            ),
        )

    def _access_token(self, open_id: str) -> str:
        cached = self._access.get(open_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        key = f"tiktok:{open_id}"
        stored = self._tokens.get(key)
        if not stored or not stored.get("refresh_token"):
            raise AnalyticsError(
                f"no TikTok authorization stored for account {open_id}: run "
                f"`hatch auth tiktok {open_id}`",
                retryable=False,
            )
        token = request_json(
            self._client,
            "POST",
            DISPLAY_TOKEN_URL,
            service="TikTok OAuth",
            data={
                "client_key": self._client_key,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
            },
        )
        if "access_token" not in token:
            raise AnalyticsError(
                f"TikTok OAuth: {token.get('error_description') or token.get('error')}",
                retryable=False,
            )
        # TikTok may rotate the refresh token: keep whichever is current.
        self._tokens.put(
            key, {**stored, "refresh_token": token.get("refresh_token", stored["refresh_token"])}
        )
        access = str(token["access_token"])
        self._access[open_id] = (access, time.monotonic() + int(token.get("expires_in", 0)) - 60)
        return access


class TikTokBusinessAnalytics:
    platform = Platform.TIKTOK
    name = "tiktok_business"
    version = "1"

    def __init__(self, *, tokens: TokenStore, client: httpx.Client | None = None) -> None:
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        stored = self._tokens.get(f"tiktok_business:{platform_account_id}")
        if not stored or not stored.get("access_token"):
            raise AnalyticsError(
                f"no TikTok Business token stored for account {platform_account_id}",
                retryable=False,
            )
        data = request_json(
            self._client,
            "GET",
            BUSINESS_VIDEO_URL,
            service="TikTok Business API",
            params={
                "business_id": platform_account_id,
                "fields": json.dumps(list(_BUSINESS_FIELDS)),
                "filters": json.dumps({"video_ids": [platform_post_id]}),
            },
            headers={"Access-Token": stored["access_token"]},
        )
        if data.get("code") != 0:
            raise AnalyticsError(
                f"TikTok Business API: {data.get('message')}", retryable=data.get("code") != 40105
            )
        videos = (data.get("data") or {}).get("videos") or []
        if not videos:
            raise AnalyticsError(f"TikTok video {platform_post_id} not found", retryable=False)
        video: dict[str, Any] = videos[0]
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw=video,
            normalized=NormalizedMetrics(
                views=video.get("video_views"),
                reach=video.get("reach"),
                likes=video.get("likes"),
                comments=video.get("comments"),
                shares=video.get("shares"),
                saves=video.get("favorites"),
                follows=video.get("new_followers"),
                average_watch_seconds=video.get("average_time_watched"),
                completion_rate=video.get("full_video_watched_rate"),
            ),
        )
