"""The Seedance 2.5 example must never report success for a request that
produced no video, and must fail clearly when credentials are missing."""

from pathlib import Path
from typing import Any

import higgsfield_client
import pytest

from examples.seedance_2_5 import main as example

COMPLETED = {
    "status": "completed",
    "request_id": "req-1",
    "video": {"url": "https://cdn.example.com/video.mp4"},
}


def test_a_completed_request_yields_its_video_url() -> None:
    assert example.video_url(COMPLETED) == "https://cdn.example.com/video.mp4"


@pytest.mark.parametrize("status", ["failed", "nsfw", "canceled"])
def test_a_request_that_ended_without_a_video_is_an_error(status: str) -> None:
    result = {"status": status, "request_id": "req-1", "error": "Generation failed"}
    with pytest.raises(example.GenerationNotCompleted, match=status):
        example.video_url(result)


def test_a_completed_request_without_a_video_url_is_an_error() -> None:
    with pytest.raises(example.GenerationNotCompleted, match="no video URL"):
        example.video_url({"status": "completed", "request_id": "req-1"})


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("HF_KEY", "HF_API_KEY", "HF_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(example, "ENV_FILE", tmp_path / ".env.local")


@pytest.mark.usefixtures("no_credentials")
def test_missing_credentials_fail_before_any_request(capsys: pytest.CaptureFixture[str]) -> None:
    assert example.main() == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert "HF_KEY" in err


def test_main_requests_the_specified_video_and_prints_its_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_subscribe(model: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        calls.append((model, arguments))
        return COMPLETED

    monkeypatch.setattr(higgsfield_client, "subscribe", fake_subscribe)
    assert example.main() == 0
    assert calls == [
        (
            "bytedance/seedance-2.5/text-to-video",
            {
                "prompt": "A cinematic scene at sunset",
                "duration": 5,
                "resolution": "720p",
                "aspect_ratio": "16:9",
            },
        )
    ]
    assert capsys.readouterr().out.strip() == "https://cdn.example.com/video.mp4"


def test_main_does_not_claim_success_for_a_moderated_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        higgsfield_client,
        "subscribe",
        lambda *_, **__: {"status": "nsfw", "request_id": "req-1"},
    )
    assert example.main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "nsfw" in err


def test_main_reports_an_api_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def rejected(*_: Any, **__: Any) -> dict[str, Any]:
        raise higgsfield_client.HiggsfieldClientError("Insufficient credits")

    monkeypatch.setattr(higgsfield_client, "subscribe", rejected)
    assert example.main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "Insufficient credits" in err
