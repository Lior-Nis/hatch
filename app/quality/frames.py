"""Sample still frames from a video for visual review."""

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.production.probe import probe_media


class Frame(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    at_seconds: float


def sample_frames(
    video: Path, workdir: Path, *, count: int = 8, max_height: int = 768
) -> list[Frame]:
    """``count`` JPEG frames at evenly spaced moments (the middle of each of
    ``count`` equal slices), scaled down to at most ``max_height`` pixels."""
    duration = probe_media(video).duration_seconds
    workdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index in range(count):
        at = (index + 0.5) * duration / count
        path = workdir / f"frame-{index + 1:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale=-2:'min(ih,{max_height})'", "-q:v", "5", str(path)],
            check=True,
            capture_output=True,
        )  # fmt: skip
        frames.append(Frame(path=path, at_seconds=at))
    return frames
