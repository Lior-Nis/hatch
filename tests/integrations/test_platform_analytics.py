"""Platform analytics adapters against stubbed official-API responses
(shapes from docs/research/platform-analytics.md). No network."""

import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.analytics.ports import AnalyticsAdapter, AnalyticsError
from app.platforms import Platform
from integrations.credentials import TokenStore
from integrations.meta.analytics import FacebookReelsAnalytics, InstagramReelsAnalytics
from integrations.tiktok.analytics import TikTokBusinessAnalytics, TikTokDisplayAnalytics
from integrations.youtube.analytics import YouTubeAnalytics


class Stub:
    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, f"{request.url.host}{request.url.path}"))
        if route is None:
            raise AssertionError(f"unexpected call: {request.method} {request.url}")
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(200, json=route)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def calls_to(self, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if fragment in str(r.url)]


@pytest.fixture
def tokens(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "credentials.json")


# --- token store -----------------------------------------------------------------


def test_token_store_persists_tokens_in_an_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "credentials.json"
    TokenStore(path).put("youtube:UC1", {"refresh_token": "r1"})

    assert TokenStore(path).get("youtube:UC1") == {"refresh_token": "r1"}
    assert TokenStore(path).get("youtube:UC2") is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- YouTube -----------------------------------------------------------------------

YT_TOKEN = ("POST", "oauth2.googleapis.com/token")
YT_VIDEOS = ("GET", "www.googleapis.com/youtube/v3/videos")
YT_REPORTS = ("GET", "youtubeanalytics.googleapis.com/v2/reports")
YT_ANALYTICS = {
    "columnHeaders": [
        {"name": "views"}, {"name": "engagedViews"}, {"name": "averageViewDuration"},
        {"name": "averageViewPercentage"}, {"name": "likes"}, {"name": "shares"},
        {"name": "subscribersGained"},
    ],
    "rows": [[1400, 900, 6.4, 80.0, 41, 7, 3]],
}  # fmt: skip
YT_VIDEO = {
    "items": [
        {"id": "vid1", "statistics": {"viewCount": "1500", "likeCount": "42", "commentCount": "0"}}
    ]
}


def youtube(stub: Stub, tokens: TokenStore) -> YouTubeAnalytics:
    tokens.put("youtube:UC1", {"refresh_token": "refresh-1"})
    return YouTubeAnalytics(
        client_id="cid", client_secret="csecret", tokens=tokens, client=stub.client()
    )


def test_youtube_combines_live_counters_with_lagged_analytics(tokens: TokenStore) -> None:
    stub = Stub({YT_TOKEN: {"access_token": "at-1", "expires_in": 3600}, YT_VIDEOS: YT_VIDEO,
                 YT_REPORTS: YT_ANALYTICS})  # fmt: skip
    adapter: AnalyticsAdapter = youtube(stub, tokens)

    observation = adapter.fetch_post_metrics(platform_account_id="UC1", platform_post_id="vid1")

    assert adapter.platform is Platform.YOUTUBE_SHORTS
    assert observation.raw["data_api"]["statistics"]["viewCount"] == "1500"
    assert observation.raw["analytics"]["rows"] == [[1400, 900, 6.4, 80.0, 41, 7, 3]]
    normalized = observation.normalized
    assert normalized.views == 1500  # the live counter
    assert normalized.likes == 42
    assert normalized.shares == 7
    assert normalized.follows == 3
    assert normalized.average_watch_seconds == 6.4
    assert normalized.average_watch_fraction == pytest.approx(0.8)
    assert normalized.completion_rate is None and normalized.saves is None
    report = stub.calls_to("youtubeanalytics")[0]
    assert report.url.params["ids"] == "channel==UC1"
    assert report.url.params["filters"] == "video==vid1"
    assert report.headers["Authorization"] == "Bearer at-1"


def test_youtube_before_analytics_catch_up_reports_counters_only(tokens: TokenStore) -> None:
    stub = Stub({YT_TOKEN: {"access_token": "at-1", "expires_in": 3600}, YT_VIDEOS: YT_VIDEO,
                 YT_REPORTS: {**YT_ANALYTICS, "rows": []}})  # fmt: skip

    normalized = (
        youtube(stub, tokens)
        .fetch_post_metrics(platform_account_id="UC1", platform_post_id="vid1")
        .normalized
    )

    assert normalized.views == 1500
    assert normalized.average_watch_fraction is None and normalized.shares is None


def test_youtube_reuses_its_access_token_between_calls(tokens: TokenStore) -> None:
    stub = Stub({YT_TOKEN: {"access_token": "at-1", "expires_in": 3600}, YT_VIDEOS: YT_VIDEO,
                 YT_REPORTS: YT_ANALYTICS})  # fmt: skip
    adapter = youtube(stub, tokens)

    adapter.fetch_post_metrics(platform_account_id="UC1", platform_post_id="vid1")
    adapter.fetch_post_metrics(platform_account_id="UC1", platform_post_id="vid1")

    assert len(stub.calls_to("oauth2.googleapis.com")) == 1


def test_youtube_without_a_stored_token_explains_what_to_run(tokens: TokenStore) -> None:
    adapter = YouTubeAnalytics(
        client_id="cid", client_secret="cs", tokens=tokens, client=Stub({}).client()
    )

    with pytest.raises(AnalyticsError, match="hatch auth youtube") as error:
        adapter.fetch_post_metrics(platform_account_id="UC1", platform_post_id="vid1")

    assert error.value.retryable is False


def test_youtube_unknown_video_and_server_errors(tokens: TokenStore) -> None:
    missing = Stub({YT_TOKEN: {"access_token": "at", "expires_in": 3600}, YT_VIDEOS: {"items": []}})
    down = Stub({YT_TOKEN: {"access_token": "at", "expires_in": 3600},
                 YT_VIDEOS: httpx.Response(503, text="backend error")})  # fmt: skip

    with pytest.raises(AnalyticsError, match="not found") as not_found:
        youtube(missing, tokens).fetch_post_metrics(platform_account_id="UC1", platform_post_id="x")
    with pytest.raises(AnalyticsError) as outage:
        youtube(down, tokens).fetch_post_metrics(platform_account_id="UC1", platform_post_id="x")

    assert not_found.value.retryable is False
    assert outage.value.retryable is True


# --- TikTok --------------------------------------------------------------------------


def test_tiktok_display_api_reports_public_counters_and_rotates_its_refresh_token(
    tokens: TokenStore,
) -> None:
    tokens.put("tiktok:open-1", {"refresh_token": "old-refresh"})
    stub = Stub({
        ("POST", "open.tiktokapis.com/v2/oauth/token/"): {
            "access_token": "tt-at", "refresh_token": "new-refresh", "expires_in": 86400,
        },
        ("POST", "open.tiktokapis.com/v2/video/query/"): {
            "data": {"videos": [{"id": "741", "view_count": 5200, "like_count": 310,
                                 "comment_count": 12, "share_count": 44, "duration": 8}]},
            "error": {"code": "ok"},
        },
    })  # fmt: skip
    adapter = TikTokDisplayAnalytics(
        client_key="ck", client_secret="cs", tokens=tokens, client=stub.client()
    )

    observation = adapter.fetch_post_metrics(platform_account_id="open-1", platform_post_id="741")

    assert adapter.platform is Platform.TIKTOK
    normalized = observation.normalized
    assert (normalized.views, normalized.likes, normalized.comments, normalized.shares) == (
        5200, 310, 12, 44,
    )  # fmt: skip
    assert normalized.completion_rate is None and normalized.average_watch_seconds is None
    assert tokens.get("tiktok:open-1")["refresh_token"] == "new-refresh"  # type: ignore[index]
    query = stub.calls_to("video/query")[0]
    assert json.loads(query.content) == {"filters": {"video_ids": ["741"]}}
    assert "view_count" in query.url.params["fields"]
    form = parse_qs(stub.calls_to("oauth/token")[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["old-refresh"]


def test_tiktok_business_api_adds_watch_time_completion_and_followers(tokens: TokenStore) -> None:
    tokens.put("tiktok_business:biz-1", {"access_token": "biz-at"})
    stub = Stub({
        ("GET", "business-api.tiktok.com/open_api/v1.3/business/video/list/"): {
            "code": 0,
            "data": {"videos": [{
                "item_id": "741", "video_views": 5200, "likes": 310, "comments": 12, "shares": 44,
                "favorites": 20, "reach": 4100, "average_time_watched": 6.1,
                "full_video_watched_rate": 0.41, "new_followers": 9, "video_duration": 8.0,
            }]},
        }
    })  # fmt: skip
    adapter = TikTokBusinessAnalytics(tokens=tokens, client=stub.client())

    normalized = adapter.fetch_post_metrics(
        platform_account_id="biz-1", platform_post_id="741"
    ).normalized

    assert normalized.views == 5200 and normalized.reach == 4100
    assert normalized.saves == 20 and normalized.follows == 9
    assert normalized.average_watch_seconds == 6.1
    assert normalized.completion_rate == 0.41
    request = stub.requests[0]
    assert request.headers["Access-Token"] == "biz-at"
    assert json.loads(request.url.params["filters"]) == {"video_ids": ["741"]}


def test_tiktok_api_level_errors_are_raised(tokens: TokenStore) -> None:
    tokens.put("tiktok_business:biz-1", {"access_token": "biz-at"})
    stub = Stub({
        ("GET", "business-api.tiktok.com/open_api/v1.3/business/video/list/"): {
            "code": 40105, "message": "Access token is incorrect or has been revoked.",
        }
    })  # fmt: skip

    with pytest.raises(AnalyticsError, match="revoked"):
        TikTokBusinessAnalytics(tokens=tokens, client=stub.client()).fetch_post_metrics(
            platform_account_id="biz-1", platform_post_id="741"
        )


# --- Instagram -------------------------------------------------------------------------


def insights(**values: Any) -> dict[str, Any]:
    return {
        "data": [{"name": name, "values": [{"value": value}]} for name, value in values.items()]
    }


def test_instagram_resolves_the_shortcode_then_reads_reel_insights(tokens: TokenStore) -> None:
    tokens.put("instagram:ig-1", {"access_token": "page-token"})
    stub = Stub({
        ("GET", "graph.facebook.com/v25.0/ig-1/media"): {
            "data": [{"id": "111", "shortcode": "other"}, {"id": "17900", "shortcode": "C8abc"}]
        },
        ("GET", "graph.facebook.com/v25.0/17900/insights"): insights(
            views=3100, reach=2500, ig_reels_avg_watch_time=5600,
            ig_reels_video_view_total_time=17_360_000, likes=140, comments=9, shares=30, saved=22,
            reels_skip_rate=0.31,
        ),
    })  # fmt: skip
    adapter = InstagramReelsAnalytics(tokens=tokens, client=stub.client())

    observation = adapter.fetch_post_metrics(platform_account_id="ig-1", platform_post_id="C8abc")

    assert adapter.platform is Platform.INSTAGRAM_REELS
    normalized = observation.normalized
    assert normalized.views == 3100 and normalized.reach == 2500
    assert normalized.average_watch_seconds == pytest.approx(5.6)  # reported in milliseconds
    assert (normalized.likes, normalized.comments, normalized.shares, normalized.saves) == (
        140, 9, 30, 22,
    )  # fmt: skip
    assert normalized.follows is None and normalized.completion_rate is None
    assert observation.raw["media_id"] == "17900"
    assert observation.raw["insights"]["reels_skip_rate"] == 0.31
    assert stub.calls_to("/insights")[0].url.params["access_token"] == "page-token"


def test_instagram_reel_that_cannot_be_found_is_a_permanent_error(tokens: TokenStore) -> None:
    tokens.put("instagram:ig-1", {"access_token": "page-token"})
    stub = Stub({("GET", "graph.facebook.com/v25.0/ig-1/media"): {"data": []}})

    with pytest.raises(AnalyticsError, match="C8abc") as error:
        InstagramReelsAnalytics(tokens=tokens, client=stub.client()).fetch_post_metrics(
            platform_account_id="ig-1", platform_post_id="C8abc"
        )

    assert error.value.retryable is False


# --- Facebook ----------------------------------------------------------------------------


def test_facebook_reel_insights_are_normalized(tokens: TokenStore) -> None:
    tokens.put("facebook:page-1", {"access_token": "page-token"})
    stub = Stub({
        ("GET", "graph.facebook.com/v25.0/998/video_insights"): insights(
            blue_reels_play_count=2100,
            post_video_avg_time_watched=4900,
            post_video_view_time=10_290_000,
            post_video_likes_by_reaction_type={"REACTION_LIKE": 70, "REACTION_LOVE": 12},
            post_video_social_actions={"COMMENT": 5, "SHARE": 18},
            post_video_followers=4,
        )
    })  # fmt: skip
    adapter = FacebookReelsAnalytics(tokens=tokens, client=stub.client())

    normalized = adapter.fetch_post_metrics(
        platform_account_id="page-1", platform_post_id="998"
    ).normalized

    assert adapter.platform is Platform.FACEBOOK_REELS
    assert normalized.views == 2100
    assert normalized.average_watch_seconds == pytest.approx(4.9)
    assert normalized.likes == 82
    assert (normalized.comments, normalized.shares, normalized.follows) == (5, 18, 4)
    assert normalized.saves is None


def test_graph_api_errors_are_classified(tokens: TokenStore) -> None:
    tokens.put("facebook:page-1", {"access_token": "page-token"})
    expired = Stub({("GET", "graph.facebook.com/v25.0/998/video_insights"): httpx.Response(
        400, json={"error": {"message": "Error validating access token", "code": 190}}
    )})  # fmt: skip
    throttled = Stub({("GET", "graph.facebook.com/v25.0/998/video_insights"): httpx.Response(
        400, json={"error": {"message": "Application request limit reached", "code": 4}}
    )})  # fmt: skip

    def fetch(stub: Stub) -> None:
        FacebookReelsAnalytics(tokens=tokens, client=stub.client()).fetch_post_metrics(
            platform_account_id="page-1", platform_post_id="998"
        )

    with pytest.raises(AnalyticsError, match="access token") as auth:
        fetch(expired)
    with pytest.raises(AnalyticsError) as limit:
        fetch(throttled)

    assert auth.value.retryable is False
    assert limit.value.retryable is True
    assert "page-token" not in str(auth.value)
