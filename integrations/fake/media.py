"""Fake media generator: deterministic, free, and scriptable.

Renders a real (tiny) test-pattern MP4 with ffmpeg so downstream code — storage,
probing, technical QA, review — runs against genuine media without paid calls.
"""

import subprocess
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
    """Write a small synthetic H.264 MP4 (test pattern, optional sine tone)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
               f"testsrc2=size={width}x{height}:rate=24:duration={duration_seconds}"]  # fmt: skip
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
    ) -> None:
        self._cost_per_second = cost_per_second_usd
        self._fail_next = list(fail_next or [])
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
        try:
            return self._jobs[provider_job_id]
        except KeyError:
            raise ProviderError(f"unknown job: {provider_job_id}") from None

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
