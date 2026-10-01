"""Technical QA against real media files rendered by ffmpeg."""

import random
import subprocess
from pathlib import Path

import pytest

from app.experiments.fixtures import FIRST_SHORT
from app.quality.ports import QACandidate, QAGate, QAOutcome, QAVerdict
from app.quality.technical import TechnicalQAGate
from integrations.fake.media import render_test_video

REQUIREMENTS = FIRST_SHORT.output.model_dump(mode="json")


def evaluate(path: Path, **requirement_overrides: object) -> QAVerdict:
    candidate = QACandidate(
        experiment_id="exp-1",
        media_path=path,
        genes={},
        creative_spec="",
        requirements={**REQUIREMENTS, **requirement_overrides},
    )
    return TechnicalQAGate().evaluate(candidate)


def failed_checks(verdict: QAVerdict) -> set[str]:
    failures = verdict.details["failures"]
    assert isinstance(failures, list)
    return {str(failure["check"]) for failure in failures if isinstance(failure, dict)}


@pytest.fixture(scope="module")
def valid_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("media") / "valid.mp4"
    render_test_video(path, width=1080, height=1920, duration_seconds=5.0)
    return path


def test_gate_is_mandatory_and_versioned() -> None:
    gate: QAGate = TechnicalQAGate()

    assert (gate.name, gate.mandatory) == ("technical", True)
    assert gate.version


def test_valid_vertical_short_passes(valid_video: Path) -> None:
    verdict = evaluate(valid_video)

    assert verdict.outcome is QAOutcome.PASS
    assert verdict.reasons == ()
    assert verdict.details["failures"] == []
    assert verdict.details["media"]["height"] == 1920  # type: ignore[index,call-overload]


def test_landscape_video_fails_the_aspect_ratio_check(tmp_path: Path) -> None:
    video = tmp_path / "landscape.mp4"
    render_test_video(video, width=1920, height=1080, duration_seconds=5.0)

    verdict = evaluate(video)

    assert verdict.outcome is QAOutcome.FAIL
    assert "aspect_ratio" in failed_checks(verdict)
    assert any("16:9" in reason for reason in verdict.reasons)


def test_low_resolution_video_fails_the_resolution_check(tmp_path: Path) -> None:
    video = tmp_path / "small.mp4"
    render_test_video(video, width=270, height=480, duration_seconds=5.0)

    assert failed_checks(evaluate(video)) == {"resolution"}


@pytest.mark.parametrize("duration", [1.0, 20.0])
def test_duration_outside_the_required_range_fails(tmp_path: Path, duration: float) -> None:
    video = tmp_path / "duration.mp4"
    render_test_video(video, duration_seconds=duration)

    assert failed_checks(evaluate(video)) == {"duration"}


def test_missing_audio_fails_only_when_audio_is_expected(tmp_path: Path) -> None:
    video = tmp_path / "silent.mp4"
    render_test_video(video, duration_seconds=5.0, with_audio=False)

    assert failed_checks(evaluate(video)) == {"audio"}
    assert evaluate(video, audio_expected=False).outcome is QAOutcome.PASS


def test_blank_video_fails_the_blank_frames_check(tmp_path: Path) -> None:
    video = tmp_path / "black.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=black:size=1080x1920:rate=12:duration=5", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=5", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
        check=True,
    )  # fmt: skip

    verdict = evaluate(video)

    assert failed_checks(verdict) == {"blank_frames"}
    assert verdict.scores["blank_fraction"] > 0.9


def test_file_that_is_not_media_fails_as_invalid(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a video")

    verdict = evaluate(junk)

    assert verdict.outcome is QAOutcome.FAIL
    assert failed_checks(verdict) == {"valid_media"}


def test_corrupted_video_stream_fails_the_decode_check(valid_video: Path, tmp_path: Path) -> None:
    data = bytearray(valid_video.read_bytes())
    start = len(data) // 2
    data[start : start + 20_000] = random.Random(0).randbytes(20_000)
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(bytes(data))

    verdict = evaluate(corrupt)

    assert verdict.outcome is QAOutcome.FAIL
    assert "decode" in failed_checks(verdict)


def test_truncated_video_fails_the_decode_check(valid_video: Path, tmp_path: Path) -> None:
    data = valid_video.read_bytes()
    truncated = tmp_path / "truncated.mp4"
    truncated.write_bytes(data[: len(data) * 6 // 10])

    assert "decode" in failed_checks(evaluate(truncated))


def test_missing_file_fails_instead_of_raising(tmp_path: Path) -> None:
    verdict = evaluate(tmp_path / "does-not-exist.mp4")

    assert verdict.outcome is QAOutcome.FAIL
    assert failed_checks(verdict) == {"valid_media"}
