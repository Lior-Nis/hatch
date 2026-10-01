"""Fake media generator: deterministic, free, and scriptable.

Renders a real (tiny) test-pattern MP4 with ffmpeg so downstream code — storage,
probing, technical QA, review — runs against genuine media without paid calls.
"""

import os
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path

from app.production.ports import (
    CostEstimate,
    GenerationJob,
    JobState,
    MediaOutput,
    MediaRequest,
    ProviderError,
)

_ASPECT_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}


def render_test_video(
    destination: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    duration_seconds: float = 2.0,
    with_audio: bool = True,
) -> None:
    """Write a small synthetic H.264 MP4 (test pattern, optional sine tone).

    Renders are deterministic, so identical requests are served from a cache in
    the system temp directory instead of re-encoding."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    cached = (
        Path(tempfile.gettempdir())
        / "hatch-fake-media"
        / f"{width}x{height}-{duration_seconds}-{'a' if with_audio else 'n'}.mp4"
    )
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        partial = cached.with_suffix(f".{os.getpid()}.tmp.mp4")
        _encode(partial, width, height, duration_seconds, with_audio)
        partial.replace(cached)
    shutil.copyfile(cached, destination)


def _encode(
    destination: Path, width: int, height: int, duration_seconds: float, with_audio: bool
) -> None:
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={width}x{height}:rate=12:duration={duration_seconds}"]  # fmt: skip
    if with_audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration_seconds}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac", "-shortest"]
    command += ["-movflags", "+faststart", str(destination)]
    subprocess.run(command, check=True, capture_output=True)


class FakeMediaGenerator:
    provider = "fake"

    def __init__(
        self,
        *,
        cost_per_second_usd: Decimal = Decimal("0.01"),
        fail_next: list[str] | None = None,
        polls_until_done: int = 0,
    ) -> None:
        self._cost_per_second = cost_per_second_usd
        self._fail_next = list(fail_next or [])
        self._polls_until_done = polls_until_done
        self._polls: dict[str, int] = {}
        self._jobs: dict[str, GenerationJob] = {}
        self._job_by_key: dict[str, str] = {}
        self._request_by_job: dict[str, MediaRequest] = {}
        self.submitted_requests: list[MediaRequest] = []

    def estimate_cost(self, request: MediaRequest) -> CostEstimate:
        return CostEstimate(amount_usd=self._cost_per_second * request.duration_seconds)

    def submit(self, request: MediaRequest) -> GenerationJob:
        existing = self._job_by_key.get(request.idempotency_key)
        if existing is not None:
            return self._jobs[existing]

        self.submitted_requests.append(request)
        job_id = f"fake-job-{len(self.submitted_requests)}"
        if self._fail_next:
            finished = GenerationJob(
                provider=self.provider,
                model=request.model,
                provider_job_id=job_id,
                state=JobState.FAILED,
                actual_cost_usd=Decimal("0"),
                error=self._fail_next.pop(0),
            )
        else:
            finished = GenerationJob(
                provider=self.provider,
                model=request.model,
                provider_job_id=job_id,
                state=JobState.SUCCEEDED,
                outputs=(MediaOutput(url=f"fake://{job_id}/video.mp4"),),
                actual_cost_usd=self.estimate_cost(request).amount_usd,
            )
        self._jobs[job_id] = finished
        self._job_by_key[request.idempotency_key] = job_id
        self._request_by_job[job_id] = request
        return finished.model_copy(update={"state": JobState.PENDING, "outputs": ()})

    def get_job(self, provider_job_id: str) -> GenerationJob:
        job = self._jobs.get(provider_job_id)
        if job is None:
            raise ProviderError(f"unknown job: {provider_job_id}")
        polls = self._polls.get(provider_job_id, 0)
        self._polls[provider_job_id] = polls + 1
        if polls < self._polls_until_done:
            return job.model_copy(
                update={"state": JobState.RUNNING, "outputs": (), "actual_cost_usd": None}
            )
        return job

    def download(self, output: MediaOutput, destination: Path) -> None:
        job_id = output.url.removeprefix("fake://").split("/", 1)[0]
        request = self._request_by_job.get(job_id)
        if request is None:
            raise ProviderError(f"unknown output: {output.url}")
        width, height = _ASPECT_SIZES.get(request.aspect_ratio, (1080, 1920))
        render_test_video(
            destination,
            width=width,
            height=height,
            duration_seconds=float(request.duration_seconds),
            with_audio=request.with_audio,
        )
