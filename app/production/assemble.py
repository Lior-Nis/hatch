"""Assemble scene clips into one video with ffmpeg."""

import subprocess
from pathlib import Path

from app.production.ports import ProductionError
from app.production.probe import probe_media


def concat_videos(sources: list[Path], destination: Path) -> None:
    """Concatenate clips in order, normalised to the first clip's frame size at
    30 fps. Audio is kept only if every clip has it."""
    infos = [probe_media(source) for source in sources]
    width, height = infos[0].width, infos[0].height
    with_audio = all(info.has_audio for info in infos)

    command = ["ffmpeg", "-y", "-loglevel", "error"]
    for source in sources:
        command += ["-i", str(source)]
    filters, joined = [], ""
    for i in range(len(sources)):
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]"
        )
        joined += f"[v{i}]"
        if with_audio:
            filters.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo[a{i}]")
            joined += f"[a{i}]"
    outputs = "[v][a]" if with_audio else "[v]"
    filters.append(f"{joined}concat=n={len(sources)}:v=1:a={1 if with_audio else 0}{outputs}")
    command += ["-filter_complex", ";".join(filters), "-map", "[v]"]
    if with_audio:
        command += ["-map", "[a]", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(destination)]  # fmt: skip
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ProductionError(f"could not assemble scenes: {completed.stderr.strip()[:500]}")
