"""One-time authorisation helpers (token exchange only; no browser, no network)."""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.analytics.ports import AnalyticsError
from integrations.credentials import TokenStore
from integrations.meta.auth import store_page_tokens
from integrations.tiktok.auth import exchange_tiktok_code, tiktok_authorize_url
from integrations.youtube.auth import exchange_youtube_code, youtube_authorize_url


def client(routes: dict[str, object]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        route = routes[f"{request.url.host}{request.url.path}"]
        return route if isinstance(route, httpx.Response) else httpx.Response(200, json=route)

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture
def tokens(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "credentials.json")


def test_youtube_consent_url_requests_offline_read_only_access() -> None:
    url = urlparse(youtube_authorize_url("cid", "http://127.0.0.1:8765", state="s1"))
    query = parse_qs(url.query)

    assert url.netloc == "accounts.google.com"
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert "yt-analytics.readonly" in query["scope"][0]
    assert "youtube.readonly" in query["scope"][0]
    assert query["state"] == ["s1"]


def test_youtube_code_is_exchanged_and_stored_for_the_authorised_channel(
    tokens: TokenStore,
) -> None:
    http = client({
        "oauth2.googleapis.com/token": {"access_token": "at", "refresh_token": "rt-1"},
        "www.googleapis.com/youtube/v3/channels": {
            "items": [{"id": "UC1", "snippet": {"title": "Nibbin Hollow"}}]
        },
    })  # fmt: skip

    channel = exchange_youtube_code(
        "code-1", client_id="cid", client_secret="cs", redirect_uri="http://127.0.0.1:8765",
        tokens=tokens, client=http,
    )  # fmt: skip

    assert channel == ("UC1", "Nibbin Hollow")
    assert tokens.get("youtube:UC1") == {"refresh_token": "rt-1"}


def test_youtube_exchange_without_a_refresh_token_is_an_error(tokens: TokenStore) -> None:
    http = client({"oauth2.googleapis.com/token": {"access_token": "at"}})

    with pytest.raises(AnalyticsError, match="refresh token"):
        exchange_youtube_code(
            "code-1", client_id="cid", client_secret="cs", redirect_uri="http://127.0.0.1:8765",
            tokens=tokens, client=http,
        )  # fmt: skip


def test_tiktok_code_is_exchanged_and_stored_under_the_account_open_id(tokens: TokenStore) -> None:
    url = tiktok_authorize_url("ck", "https://example.com/cb", state="s1")
    http = client({
        "open.tiktokapis.com/v2/oauth/token/": {
            "access_token": "at", "refresh_token": "rt-9", "open_id": "open-1", "expires_in": 86400,
        }
    })  # fmt: skip

    open_id = exchange_tiktok_code(
        "code-1", client_key="ck", client_secret="cs", redirect_uri="https://example.com/cb",
        tokens=tokens, client=http,
    )  # fmt: skip

    assert "video.list" in parse_qs(urlparse(url).query)["scope"][0]
    assert open_id == "open-1"
    assert tokens.get("tiktok:open-1") == {"refresh_token": "rt-9"}


def test_meta_user_token_becomes_stored_page_and_instagram_tokens(tokens: TokenStore) -> None:
    http = client({
        "graph.facebook.com/v25.0/oauth/access_token": {"access_token": "long-lived-user"},
        "graph.facebook.com/v25.0/me/accounts": {
            "data": [
                {"id": "page-1", "name": "Nibbin Hollow", "access_token": "page-token-1",
                 "instagram_business_account": {"id": "ig-1"}},
                {"id": "page-2", "name": "Other", "access_token": "page-token-2"},
            ]
        },
    })  # fmt: skip

    pages = store_page_tokens(
        "short-user-token", app_id="app", app_secret="secret", tokens=tokens, client=http
    )

    assert pages == [("page-1", "Nibbin Hollow", "ig-1"), ("page-2", "Other", None)]
    assert tokens.get("facebook:page-1") == {"access_token": "page-token-1"}
    assert tokens.get("instagram:ig-1") == {"access_token": "page-token-1"}
    assert tokens.get("facebook:page-2") == {"access_token": "page-token-2"}
