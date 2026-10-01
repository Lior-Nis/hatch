"""Higgsfield API adapter for the ``MediaGenerator`` port.

Wraps the prepaid HTTP API (``api.higgsfield.ai``, ``Authorization: Key id:secret``)
rather than the CLI: it authenticates without a browser, prices a request in USD
before submission, honours ``Idempotency-Key``, and documents that ``failed`` and
``nsfw`` requests are not charged. See docs/research/higgsfield-api.md.

All Higgsfield vocabulary (endpoint ids, body field names, status strings) stays
in this module.
"""

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from app.production.ports import (
    CostEstimate,
    GenerationJob,
    JobState,
    MediaOutput,
    MediaRequest,
    ProviderError,
)

DEFAULT_BASE_URL = "https://api.higgsfield.ai"

_STATE_BY_STATUS = {
    "queued": JobState.PENDING,
    "in_progress": JobState.RUNNING,
    "completed": JobState.SUCCEEDED,
    "failed": JobState.FAILED,
    "nsfw": JobState.FAILED,
    "canceled": JobState.FAILED,
}

# How each endpoint family spells "generate native audio".
_AUDIO_FIELD_BY_PREFIX = {
    "kling-video/v3.0-turbo/": None,
    "kling-video/v2.5-turbo/": None,
    "kling-video/": "sound",
    "minimax/": None,
    "wan/": None,
}
_DEFAULT_AUDIO_FIELD = "generate_audio"


def _audio_field(model: str) -> str | None:
    for prefix, field in _AUDIO_FIELD_BY_PREFIX.items():
        if model.startswith(prefix):
            return field
    return _DEFAULT_AUDIO_FIELD


def _request_body(request: MediaRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt": request.prompt,
        "duration": request.duration_seconds,
        "aspect_ratio": request.aspect_ratio,
    }
    if request.resolution not in (None, "default"):
        # Always explicit: some endpoints default to their most expensive tier.
        body["resolution"] = request.resolution
    if (audio_field := _audio_field(request.model)) is not None:
        body[audio_field] = request.with_audio
    body.update(request.extra)
    return body


class HiggsfieldMediaGenerator:
    provider = "higgsfield"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Higgsfield API credentials are not configured")
        self._auth = {"Authorization": f"Key {api_key}:{api_secret}"}
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0))

    def estimate_cost(self, request: MediaRequest) -> CostEstimate:
        data = self._call("POST", f"/estimate/{request.model}", json=_request_body(request))
        try:
            return CostEstimate(
                amount_usd=Decimal(str(data["usd"])),
                provider_units=Decimal(str(data["credits"])) if "credits" in data else None,
                provider_unit_name="credits",
            )
        except (KeyError, InvalidOperation) as exc:
            raise ProviderError(f"unexpected Higgsfield estimate response: {data}") from exc

    def submit(self, request: MediaRequest) -> GenerationJob:
        data = self._call(
            "POST",
            f"/{request.model}",
            json=_request_body(request),
            headers={"Idempotency-Key": request.idempotency_key},
        )
        request_id = data.get("request_id")
        if not isinstance(request_id, str):
            raise ProviderError(f"Higgsfield submit returned no request_id: {data}")
        return GenerationJob(
            provider=self.provider,
            model=request.model,
            provider_job_id=request_id,
            state=_STATE_BY_STATUS.get(str(data.get("status")), JobState.PENDING),
            raw=data,
        )

    def get_job(self, provider_job_id: str) -> GenerationJob:
        data = self._call("GET", f"/requests/{provider_job_id}/status")
        status = str(data.get("status"))
        state = _STATE_BY_STATUS.get(status)
        if state is None:
            raise ProviderError(f"unknown Higgsfield status {status!r} for {provider_job_id}")

        outputs: tuple[MediaOutput, ...] = ()
        error: str | None = None
        actual_cost: Decimal | None = None
        if state is JobState.SUCCEEDED:
            url = (data.get("video") or {}).get("url")
            if isinstance(url, str):
                outputs = (MediaOutput(url=url),)
        elif state is JobState.FAILED:
            detail = data.get("error") or data.get("detail")
            error = f"{status}: {detail}" if detail else status
            actual_cost = Decimal("0")  # failed / nsfw / canceled requests are not charged
        return GenerationJob(
            provider=self.provider,
            model=str(data.get("model", "")),
            provider_job_id=provider_job_id,
            state=state,
            outputs=outputs,
            actual_cost_usd=actual_cost,
            error=error,
            raw=data,
        )

    def download(self, output: MediaOutput, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Result URLs are pre-signed CDN links: never send API credentials.
            with self._client.stream("GET", output.url, follow_redirects=True) as response:
                response.raise_for_status()
                with destination.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        stream.write(chunk)
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not download Higgsfield output: {exc}") from exc

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {**self._auth, **kwargs.pop("headers", {})}
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Higgsfield {method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"Higgsfield {method} {path} returned {response.status_code}: {response.text[:500]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"Higgsfield {method} {path} returned non-JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"Higgsfield {method} {path} returned unexpected JSON")
        return data
