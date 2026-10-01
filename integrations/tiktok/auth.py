"""One-time TikTok authorisation (Display API, Login Kit) for an owned account."""

from urllib.parse import urlencode

import httpx

from app.analytics.ports import AnalyticsError
from integrations.credentials import TokenStore
from integrations.http import request_json
from integrations.tiktok.analytics import DISPLAY_TOKEN_URL

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.list"


def tiktok_authorize_url(client_key: str, redirect_uri: str, *, state: str) -> str:
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_key": client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def exchange_tiktok_code(
    code: str,
    *,
    client_key: str,
    client_secret: str,
    redirect_uri: str,
    tokens: TokenStore,
    client: httpx.Client | None = None,
) -> str:
    """Exchange the authorisation code and store the refresh token under the
    account's ``open_id``, which is returned."""
    client = client or httpx.Client(timeout=httpx.Timeout(30.0))
    token = request_json(
        client,
        "POST",
        DISPLAY_TOKEN_URL,
        service="TikTok OAuth",
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if not token.get("refresh_token") or not token.get("open_id"):
        raise AnalyticsError(
            f"TikTok OAuth: {token.get('error_description') or token.get('error') or token}",
            retryable=False,
        )
    tokens.put(f"tiktok:{token['open_id']}", {"refresh_token": token["refresh_token"]})
    return str(token["open_id"])
