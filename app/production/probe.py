"""Inspect media files with ffprobe."""

import json
import subprocess
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class MediaProbeError(Exception):
    """The file is missing, unreadable, or not a video."""


class MediaInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_name: str
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    frame_rate: float
    has_audio: bool
    audio_codec: str | None = None


def probe_media(path: Path) -> MediaInfo:
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MediaProbeError(completed.stderr.strip() or f"ffprobe failed for {path}")
    data = json.loads(completed.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaProbeError(f"no video stream in {path}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    try:
        return MediaInfo(
            format_name=data["format"]["format_name"],
            duration_seconds=float(data["format"]["duration"]),
            width=int(video["width"]),
            height=int(video["height"]),
            video_codec=video["codec_name"],
            frame_rate=float(Fraction(video.get("avg_frame_rate") or "0/1")),
            has_audio=audio is not None,
            audio_codec=audio["codec_name"] if audio else None,
        )
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise MediaProbeError(f"incomplete media metadata for {path}: {exc}") from exc
