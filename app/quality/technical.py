"""Technical media QA: is this file a valid, complete, vertical Short?

Deterministic checks with ffprobe/ffmpeg against the experiment's output
requirements. Mandatory: a video that fails cannot proceed.
"""

import re
import subprocess
from math import gcd
from pathlib import Path
from typing import Any

from app.experiments.spec import OutputRequirements
from app.production.probe import MediaInfo, MediaProbeError, probe_media
from app.quality.ports import QACandidate, QAOutcome, QAVerdict

_BLACK_DURATION = re.compile(r"black_duration:([0-9.]+)")
# With ``-loglevel level+info`` ffmpeg tags each line: "[h264 @ 0x..] [error] ...".
_ERROR_LINE = re.compile(r"\[(error|fatal|panic)\]|\[warning\] corrupt")


def _ratio(text: str) -> float:
    width, height = text.split(":")
    return float(width) / float(height)


def _simplified(width: int, height: int) -> str:
    divisor = gcd(width, height) or 1
    return f"{width // divisor}:{height // divisor}"


class TechnicalQAGate:
    name = "technical"
    version = "1"
    mandatory = True

    def __init__(self, *, aspect_tolerance: float = 0.05, max_blank_fraction: float = 0.5) -> None:
        self._aspect_tolerance = aspect_tolerance
        self._max_blank_fraction = max_blank_fraction

    def evaluate(self, candidate: QACandidate) -> QAVerdict:
        requirements = OutputRequirements.model_validate(candidate.requirements)
        try:
            media = probe_media(candidate.media_path)
        except MediaProbeError as exc:
            return self._verdict(
                [{"check": "valid_media", "message": f"not a valid media file: {exc}"}], {}, None
            )

        failures: list[dict[str, Any]] = []
        expected_ratio = _ratio(requirements.aspect_ratio)
        actual_ratio = media.width / media.height
        if abs(actual_ratio - expected_ratio) / expected_ratio > self._aspect_tolerance:
            failures.append(
                {
                    "check": "aspect_ratio",
                    "expected": requirements.aspect_ratio,
                    "actual": _simplified(media.width, media.height),
                    "message": (
                        f"aspect ratio is {_simplified(media.width, media.height)}, "
                        f"expected {requirements.aspect_ratio}"
                    ),
                }
            )
        if media.height < requirements.min_height:
            failures.append(
                {
                    "check": "resolution",
                    "expected": f">= {requirements.min_height}px tall",
                    "actual": f"{media.width}x{media.height}",
                    "message": (
                        f"resolution {media.width}x{media.height} is below the required "
                        f"height of {requirements.min_height}px"
                    ),
                }
            )
        low, high = requirements.min_duration_seconds, requirements.max_duration_seconds
        if not low <= media.duration_seconds <= high:
            failures.append(
                {
                    "check": "duration",
                    "expected": f"{low}-{high}s",
                    "actual": round(media.duration_seconds, 2),
                    "message": (
                        f"duration {media.duration_seconds:.1f}s is outside the required "
                        f"{low:g}-{high:g}s"
                    ),
                }
            )
        if requirements.audio_expected and not media.has_audio:
            failures.append(
                {"check": "audio", "message": "audio track is missing but audio is expected"}
            )

        decode_errors, blank_seconds = self._decode(candidate.media_path)
        if decode_errors:
            failures.append(
                {
                    "check": "decode",
                    "message": f"corrupt or undecodable frames: {decode_errors[0][:200]}",
                    "error_count": len(decode_errors),
                }
            )
        blank_fraction = min(1.0, blank_seconds / media.duration_seconds)
        if blank_fraction > self._max_blank_fraction:
            failures.append(
                {
                    "check": "blank_frames",
                    "actual": round(blank_fraction, 3),
                    "message": f"{blank_fraction:.0%} of the video is blank",
                }
            )
        return self._verdict(failures, {"blank_fraction": blank_fraction}, media)

    def _decode(self, path: Path) -> tuple[list[str], float]:
        """Decode the whole file once: collect decoder errors and blank time."""
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "level+info", "-i", str(path),
             "-vf", "blackdetect=d=0.05:pix_th=0.10", "-f", "null", "-"],
            capture_output=True,
            text=True,
            errors="replace",
        )  # fmt: skip
        log = completed.stderr
        errors = [line for line in log.splitlines() if _ERROR_LINE.search(line)]
        if completed.returncode != 0 and not errors:
            errors.append(f"ffmpeg exited with status {completed.returncode}")
        blank_seconds = sum(float(value) for value in _BLACK_DURATION.findall(log))
        return errors, blank_seconds

    def _verdict(
        self, failures: list[dict[str, Any]], scores: dict[str, float], media: MediaInfo | None
    ) -> QAVerdict:
        return QAVerdict(
            gate=self.name,
            gate_version=self.version,
            outcome=QAOutcome.FAIL if failures else QAOutcome.PASS,
            scores=scores,
            reasons=tuple(f"{failure['check']}: {failure['message']}" for failure in failures),
            details={
                "failures": failures,
                "media": media.model_dump(mode="json") if media else None,
            },
        )
