"""Media generation port.

``MediaGenerator`` is the only way the domain talks to a media vendor
(Higgsfield in V1). Adapters translate these provider-neutral types to vendor
calls; vendor response objects never cross this boundary. The opaque ``raw``
payload on a job exists solely so it can be persisted for reproducibility.
"""

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ProviderError(Exception):
    """A vendor call failed in a way the caller may retry or report."""


class MediaOperation(StrEnum):
    TEXT_TO_VIDEO = "text_to_video"


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED)


class MediaRequest(BaseModel):
    """One paid generation call, described without vendor vocabulary."""

    model_config = ConfigDict(frozen=True)

    operation: MediaOperation
    model: str
    prompt: str
    duration_seconds: int = Field(gt=0)
    aspect_ratio: str = "9:16"
    resolution: str | None = None
    with_audio: bool = True
    extra: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1)
    """Stable per generation attempt. Resubmitting the same key must not start
    (or charge for) a second job."""


class CostEstimate(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount_usd: Decimal
    provider_units: Decimal | None = None
    provider_unit_name: str | None = None


class MediaOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    kind: str = "video"


class GenerationJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    provider_job_id: str
    state: JobState
    outputs: tuple[MediaOutput, ...] = ()
    actual_cost_usd: Decimal | None = None
    error: str | None = None
    raw: dict[str, JsonValue] = Field(default_factory=dict)


class MediaGenerator(Protocol):
    """Submit/poll media generation. Jobs can take minutes, so submission and
    completion are separate calls: a restarted worker resumes polling a stored
    ``provider_job_id`` instead of paying for a new generation."""

    @property
    def provider(self) -> str: ...

    def estimate_cost(self, request: MediaRequest) -> CostEstimate:
        """Price a request without starting (or charging for) a job."""
        ...

    def submit(self, request: MediaRequest) -> GenerationJob:
        """Start a job. Idempotent on ``request.idempotency_key``."""
        ...

    def get_job(self, provider_job_id: str) -> GenerationJob:
        """Current state of a job. Raises ``ProviderError`` if unknown."""
        ...

    def download(self, output: MediaOutput, destination: Path) -> None:
        """Write a finished output to ``destination``."""
        ...
