"""One-time YouTube authorisation for a channel the operator owns.

Installed-app OAuth with a loopback redirect: the operator approves read-only
access in their browser once per channel, and Hatch keeps the refresh token in
the local token store.
"""

from urllib.parse import urlencode

import httpx

from app.analytics.ports import AnalyticsError
from integrations.credentials import TokenStore
from integrations.http import request_json
from integrations.youtube.analytics import SCOPES, TOKEN_URL

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def youtube_authorize_url(client_id: str, redirect_uri: str, *, state: str) -> str:
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # always return a refresh token
            "state": state,
        }
    )


def exchange_youtube_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    tokens: TokenStore,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """Exchange the consent code, find out which channel was authorised, and
    store its refresh token. Returns ``(channel_id, channel_title)``."""
    client = client or httpx.Client(timeout=httpx.Timeout(30.0))
    token = request_json(
        client,
        "POST",
        TOKEN_URL,
        service="Google OAuth",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if not token.get("refresh_token"):
        raise AnalyticsError(
            "Google returned no refresh token; remove Hatch from the Google account's "
            "third-party access and authorise again",
            retryable=False,
        )
    channels = request_json(
        client,
        "GET",
        CHANNELS_URL,
        service="YouTube Data API",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    if not channels.get("items"):
        raise AnalyticsError(
            "the authorised Google account has no YouTube channel", retryable=False
        )
    channel = channels["items"][0]
    tokens.put(f"youtube:{channel['id']}", {"refresh_token": token["refresh_token"]})
    return channel["id"], channel.get("snippet", {}).get("title", "")
