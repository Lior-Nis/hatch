"""Shared HTTP helper for analytics adapters."""

from typing import Any

import httpx

from app.analytics.ports import AnalyticsError


def request_json(
    client: httpx.Client, method: str, url: str, *, service: str, **kwargs: Any
) -> dict[str, Any]:
    """Perform a request and return its JSON object, mapping transport and
    HTTP failures to ``AnalyticsError``. Error text never includes request
    headers or query parameters, so tokens cannot leak through it."""
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise AnalyticsError(f"{service}: network error ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        retryable = response.status_code >= 500 or response.status_code == 429
        raise AnalyticsError(
            f"{service}: HTTP {response.status_code}: {response.text[:300]}", retryable=retryable
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise AnalyticsError(f"{service}: non-JSON response") from exc
    if not isinstance(data, dict):
        raise AnalyticsError(f"{service}: unexpected response shape")
    return data
