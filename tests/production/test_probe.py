from pathlib import Path

import pytest

from app.production.probe import MediaProbeError, probe_media
from integrations.fake.media import render_test_video


def test_probe_reports_dimensions_duration_and_audio(tmp_path: Path) -> None:
    video = tmp_path / "v.mp4"
    render_test_video(video, width=1080, height=1920, duration_seconds=2.0)

    info = probe_media(video)

    assert (info.width, info.height) == (1080, 1920)
    assert info.duration_seconds == pytest.approx(2.0, abs=0.2)
    assert info.video_codec == "h264"
    assert info.has_audio is True


def test_probe_reports_missing_audio(tmp_path: Path) -> None:
    video = tmp_path / "silent.mp4"
    render_test_video(video, with_audio=False)

    assert probe_media(video).has_audio is False


def test_probe_rejects_a_file_that_is_not_media(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")

    with pytest.raises(MediaProbeError):
        probe_media(junk)
