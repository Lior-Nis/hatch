"""Adapter translation tests against the documented Higgsfield API shapes
(docs/research/higgsfield-api.md). No network: requests hit a stub transport."""

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.production.ports import (
    JobState,
    MediaGenerator,
    MediaOperation,
    MediaOutput,
    MediaRequest,
    ProviderError,
)
from integrations.higgsfield.generator import HiggsfieldMediaGenerator

MODEL = "alibaba/wan-3.0/text-to-video"


def request(**overrides: object) -> MediaRequest:
    fields: dict[str, object] = {
        "operation": MediaOperation.TEXT_TO_VIDEO,
        "model": MODEL,
        "prompt": "A hedgehog finds fireflies.",
        "duration_seconds": 8,
        "aspect_ratio": "9:16",
        "resolution": "480p",
        "with_audio": True,
        "idempotency_key": "exp-1:attempt:1",
    }
    fields.update(overrides)
    return MediaRequest.model_validate(fields)


class StubApi:
    def __init__(self, responses: dict[tuple[str, str], httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[(request.method, request.url.path)]


def generator(api: StubApi) -> HiggsfieldMediaGenerator:
    return HiggsfieldMediaGenerator(
        api_key="key-id",
        api_secret="s3cret",
        client=httpx.Client(transport=httpx.MockTransport(api)),
    )


def test_adapter_satisfies_the_media_generator_contract() -> None:
    adapter: MediaGenerator = generator(StubApi({}))
    assert adapter.provider == "higgsfield"


def test_estimate_posts_the_vendor_body_and_returns_usd() -> None:
    api = StubApi(
        {
            ("POST", f"/estimate/{MODEL}"): httpx.Response(
                200, json={"credits": "6.400", "usd": "0.400"}
            )
        }
    )

    estimate = generator(api).estimate_cost(request())

    assert estimate.amount_usd == Decimal("0.400")
    assert estimate.provider_units == Decimal("6.400")
    [sent] = api.requests
    assert sent.headers["Authorization"] == "Key key-id:s3cret"
    assert json.loads(sent.content) == {
        "prompt": "A hedgehog finds fireflies.",
        "duration": 8,
        "aspect_ratio": "9:16",
        "resolution": "480p",
        "generate_audio": True,
    }


def test_submit_sends_the_idempotency_key_and_returns_a_pending_job() -> None:
    api = StubApi(
        {
            ("POST", f"/{MODEL}"): httpx.Response(
                200,
                json={
                    "status": "queued",
                    "request_id": "req-123",
                    "status_url": "https://api.higgsfield.ai/requests/req-123/status",
                    "cancel_url": "https://api.higgsfield.ai/requests/req-123/cancel",
                },
            )
        }
    )

    job = generator(api).submit(request())

    assert job.provider == "higgsfield"
    assert job.provider_job_id == "req-123"
    assert job.state is JobState.PENDING
    assert api.requests[0].headers["Idempotency-Key"] == "exp-1:attempt:1"


def test_kling_models_receive_their_sound_parameter_instead_of_generate_audio() -> None:
    model = "kling-video/v2.6/pro/text-to-video"
    api = StubApi(
        {("POST", f"/estimate/{model}"): httpx.Response(200, json={"credits": "1", "usd": "0.35"})}
    )

    generator(api).estimate_cost(request(model=model, with_audio=False, resolution=None))

    body = json.loads(api.requests[0].content)
    assert body["sound"] is False
    assert "generate_audio" not in body
    assert "resolution" not in body


def test_extra_parameters_are_passed_through_to_the_vendor_body() -> None:
    api = StubApi(
        {("POST", f"/estimate/{MODEL}"): httpx.Response(200, json={"credits": "1", "usd": "0.4"})}
    )

    generator(api).estimate_cost(request(extra={"seed": 7}))

    assert json.loads(api.requests[0].content)["seed"] == 7


def test_completed_job_exposes_the_video_url() -> None:
    api = StubApi(
        {
            ("GET", "/requests/req-123/status"): httpx.Response(
                200,
                json={
                    "status": "completed",
                    "request_id": "req-123",
                    "video": {"url": "https://cdn.example/video.mp4"},
                },
            )
        }
    )

    job = generator(api).get_job("req-123")

    assert job.state is JobState.SUCCEEDED
    assert job.outputs == (MediaOutput(url="https://cdn.example/video.mp4"),)
    assert job.raw["status"] == "completed"


@pytest.mark.parametrize(
    ("status", "state"),
    [("queued", JobState.PENDING), ("in_progress", JobState.RUNNING)],
)
def test_unfinished_statuses_map_to_non_terminal_states(status: str, state: JobState) -> None:
    api = StubApi(
        {
            ("GET", "/requests/req-1/status"): httpx.Response(
                200, json={"status": status, "request_id": "req-1"}
            )
        }
    )

    assert generator(api).get_job("req-1").state is state


@pytest.mark.parametrize("status", ["failed", "nsfw", "canceled"])
def test_unsuccessful_terminal_statuses_fail_the_job_at_no_cost(status: str) -> None:
    api = StubApi(
        {
            ("GET", "/requests/req-1/status"): httpx.Response(
                200, json={"status": status, "request_id": "req-1", "error": "boom"}
            )
        }
    )

    job = generator(api).get_job("req-1")

    assert job.state is JobState.FAILED
    assert job.error is not None and status in job.error
    assert job.actual_cost_usd == Decimal("0")
    assert job.outputs == ()


def test_http_errors_become_provider_errors_without_leaking_the_secret() -> None:
    api = StubApi(
        {
            ("POST", f"/estimate/{MODEL}"): httpx.Response(
                401, json={"detail": "Invalid credentials"}
            )
        }
    )

    with pytest.raises(ProviderError) as error:
        generator(api).estimate_cost(request())

    assert "401" in str(error.value)
    assert "Invalid credentials" in str(error.value)
    assert "s3cret" not in str(error.value)


def test_network_failures_become_provider_errors() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = HiggsfieldMediaGenerator(
        api_key="k", api_secret="s", client=httpx.Client(transport=httpx.MockTransport(explode))
    )

    with pytest.raises(ProviderError):
        adapter.get_job("req-1")


def test_download_writes_the_video_bytes_to_disk(tmp_path: Path) -> None:
    api = StubApi({("GET", "/video.mp4"): httpx.Response(200, content=b"mp4-bytes")})
    destination = tmp_path / "nested" / "out.mp4"

    generator(api).download(MediaOutput(url="https://cdn.example/video.mp4"), destination)

    assert destination.read_bytes() == b"mp4-bytes"
    assert "Authorization" not in api.requests[0].headers


def test_missing_credentials_are_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="credentials"):
        HiggsfieldMediaGenerator(api_key="", api_secret="")
