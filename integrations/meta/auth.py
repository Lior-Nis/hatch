"""One-time Meta authorisation for Pages (and their Instagram accounts) the
operator owns: a short-lived user token is exchanged for a long-lived one, and
the non-expiring Page tokens derived from it are stored."""

import httpx

from integrations.credentials import TokenStore
from integrations.http import request_json
from integrations.meta.analytics import GRAPH_URL


def store_page_tokens(
    user_token: str,
    *,
    app_id: str,
    app_secret: str,
    tokens: TokenStore,
    client: httpx.Client | None = None,
) -> list[tuple[str, str, str | None]]:
    """Returns ``(page_id, page_name, instagram_user_id)`` for every Page the
    user manages, after storing each Page token (also under the linked
    Instagram account, whose insights are read with the same token)."""
    client = client or httpx.Client(timeout=httpx.Timeout(30.0))
    long_lived = request_json(
        client,
        "GET",
        f"{GRAPH_URL}/oauth/access_token",
        service="Graph API",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        },
    )["access_token"]
    accounts = request_json(
        client,
        "GET",
        f"{GRAPH_URL}/me/accounts",
        service="Graph API",
        params={
            "fields": "id,name,access_token,instagram_business_account",
            "access_token": long_lived,
        },
    )
    pages = []
    for page in accounts.get("data", []):
        instagram_id = (page.get("instagram_business_account") or {}).get("id")
        tokens.put(f"facebook:{page['id']}", {"access_token": page["access_token"]})
        if instagram_id:
            tokens.put(f"instagram:{instagram_id}", {"access_token": page["access_token"]})
        pages.append((page["id"], page.get("name", ""), instagram_id))
    return pages
