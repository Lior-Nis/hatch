import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.production.ports import (
    JobState,
    MediaGenerator,
    MediaOperation,
    MediaRequest,
    ProviderError,
)
from integrations.fake.media import FakeMediaGenerator


def _request(key: str = "exp-1:attempt-1") -> MediaRequest:
    return MediaRequest(
        operation=MediaOperation.TEXT_TO_VIDEO,
        model="fake-video-1",
        prompt="A friendly snail paints a rainbow.",
        duration_seconds=2,
        idempotency_key=key,
    )


def _probe(path: Path) -> dict[str, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    stream: dict[str, int] = json.loads(out.stdout)["streams"][0]
    return stream


def test_fake_generator_satisfies_the_media_generator_contract() -> None:
    generator: MediaGenerator = FakeMediaGenerator()
    assert generator.provider == "fake"


def test_estimate_is_reported_in_usd_without_starting_a_job() -> None:
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))

    estimate = generator.estimate_cost(_request())

    assert estimate.amount_usd == Decimal("0.10")
    assert generator.submitted_requests == []


def test_submitted_job_succeeds_and_downloads_a_vertical_video(tmp_path: Path) -> None:
    generator = FakeMediaGenerator()

    job = generator.submit(_request())
    finished = generator.get_job(job.provider_job_id)
    destination = tmp_path / "short.mp4"
    generator.download(finished.outputs[0], destination)

    assert finished.state is JobState.SUCCEEDED
    assert finished.actual_cost_usd is not None
    stream = _probe(destination)
    assert (stream["width"], stream["height"]) == (1080, 1920)


def test_resubmitting_the_same_idempotency_key_does_not_create_a_second_job() -> None:
    generator = FakeMediaGenerator()

    first = generator.submit(_request("exp-1:attempt-1"))
    second = generator.submit(_request("exp-1:attempt-1"))

    assert second.provider_job_id == first.provider_job_id
    assert len(generator.submitted_requests) == 1


def test_scripted_failure_is_reported_as_a_failed_job_with_an_error() -> None:
    generator = FakeMediaGenerator(fail_next=["content policy rejection"])

    job = generator.submit(_request())
    finished = generator.get_job(job.provider_job_id)

    assert finished.state is JobState.FAILED
    assert finished.error == "content policy rejection"
    assert finished.outputs == ()


def test_unknown_job_id_raises_a_provider_error() -> None:
    with pytest.raises(ProviderError):
        FakeMediaGenerator().get_job("does-not-exist")
