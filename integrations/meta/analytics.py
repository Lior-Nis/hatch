"""Meta analytics adapters: Instagram Reels and Facebook Reels (Graph API).

Both read insights with a Page access token (Facebook Login, Standard Access
for accounts the operator owns). Instagram does not report follows or
completion per Reel and Facebook does not report saves; those stay ``None``.
Watch times arrive in milliseconds and are normalized to seconds.
"""

from datetime import UTC, datetime
from typing import Any

import httpx

from app.analytics.ports import AnalyticsError, MetricObservation, NormalizedMetrics
from app.platforms import Platform
from integrations.credentials import TokenStore

GRAPH_URL = "https://graph.facebook.com/v25.0"
_INSTAGRAM_METRICS = (
    "views,reach,ig_reels_avg_watch_time,ig_reels_video_view_total_time,"
    "likes,comments,shares,saved,reels_skip_rate"
)
_FACEBOOK_METRICS = (
    "blue_reels_play_count,post_video_avg_time_watched,post_video_view_time,"
    "post_video_likes_by_reaction_type,post_video_social_actions,post_video_followers,"
    "post_video_retention_graph"
)
# Graph error codes: 190 = invalid/expired token, 10/200-range = permissions.
_AUTH_ERROR_CODES = {10, 190, 200, 803}
_THROTTLE_ERROR_CODES = {4, 17, 32, 613, 80001, 80002}


class _GraphAdapter:
    token_prefix = ""

    def __init__(self, *, tokens: TokenStore, client: httpx.Client | None = None) -> None:
        self._tokens = tokens
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))

    def _token(self, account_id: str) -> str:
        stored = self._tokens.get(f"{self.token_prefix}:{account_id}")
        if not stored or not stored.get("access_token"):
            raise AnalyticsError(
                f"no Meta access token stored for {self.token_prefix} account {account_id}: "
                "run `hatch auth meta`",
                retryable=False,
            )
        return str(stored["access_token"])

    def _get(self, path: str, token: str, **params: str) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{GRAPH_URL}/{path}", params={**params, "access_token": token}
            )
        except httpx.HTTPError as exc:
            raise AnalyticsError(f"Graph API: network error ({type(exc).__name__})") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise AnalyticsError("Graph API: non-JSON response") from exc
        if response.status_code >= 400 or "error" in data:
            error = data.get("error") or {}
            code = error.get("code")
            retryable = (
                code in _THROTTLE_ERROR_CODES
                or response.status_code >= 500
                or (code not in _AUTH_ERROR_CODES and response.status_code == 429)
            )
            raise AnalyticsError(
                f"Graph API error {code}: {error.get('message', response.status_code)}",
                retryable=retryable,
            )
        return dict(data)

    @staticmethod
    def _values(insights: dict[str, Any]) -> dict[str, Any]:
        return {
            item["name"]: (item.get("values") or [{}])[0].get("value")
            for item in insights.get("data", [])
        }


def _seconds(milliseconds: Any) -> float | None:
    return milliseconds / 1000 if isinstance(milliseconds, int | float) else None


class InstagramReelsAnalytics(_GraphAdapter):
    platform = Platform.INSTAGRAM_REELS
    name = "instagram"
    version = "1"
    token_prefix = "instagram"

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        """``platform_post_id`` is the Reel's shortcode (from its permalink);
        the numeric media id that insights need is looked up first."""
        token = self._token(platform_account_id)
        media = self._get(
            f"{platform_account_id}/media", token, fields="id,shortcode,permalink", limit="100"
        )
        media_id = next(
            (m["id"] for m in media.get("data", []) if m.get("shortcode") == platform_post_id),
            None,
        )
        if media_id is None:
            raise AnalyticsError(
                f"Instagram Reel {platform_post_id} is not among the account's recent media",
                retryable=False,
            )
        values = self._values(self._get(f"{media_id}/insights", token, metric=_INSTAGRAM_METRICS))
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw={"media_id": media_id, "insights": values},
            normalized=NormalizedMetrics(
                views=values.get("views"),
                reach=values.get("reach"),
                average_watch_seconds=_seconds(values.get("ig_reels_avg_watch_time")),
                likes=values.get("likes"),
                comments=values.get("comments"),
                shares=values.get("shares"),
                saves=values.get("saved"),
            ),
        )


class FacebookReelsAnalytics(_GraphAdapter):
    platform = Platform.FACEBOOK_REELS
    name = "facebook"
    version = "1"
    token_prefix = "facebook"

    def fetch_post_metrics(
        self, *, platform_account_id: str, platform_post_id: str
    ) -> MetricObservation:
        token = self._token(platform_account_id)
        values = self._values(
            self._get(f"{platform_post_id}/video_insights", token, metric=_FACEBOOK_METRICS)
        )
        reactions = values.get("post_video_likes_by_reaction_type")
        actions = values.get("post_video_social_actions") or {}
        return MetricObservation(
            platform=self.platform,
            platform_post_id=platform_post_id,
            observed_at=datetime.now(UTC),
            raw={"insights": values},
            normalized=NormalizedMetrics(
                views=values.get("blue_reels_play_count"),
                average_watch_seconds=_seconds(values.get("post_video_avg_time_watched")),
                likes=sum(reactions.values()) if isinstance(reactions, dict) else None,
                comments=actions.get("COMMENT"),
                shares=actions.get("SHARE"),
                follows=values.get("post_video_followers"),
            ),
        )
