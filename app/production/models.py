"""Generation attempts and the assets they produce."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict, Money, enum_column
from app.experiments.models import Experiment


class AttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED_BUDGET = "blocked_budget"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AssetKind(StrEnum):
    STORYBOARD = "storyboard"
    REFERENCE_IMAGE = "reference_image"
    RAW_VIDEO = "raw_video"
    AUDIO = "audio"
    FINAL_VIDEO = "final_video"
    THUMBNAIL = "thumbnail"
    QA_ARTIFACT = "qa_artifact"


class ProductionStep(Identified, Base):
    """One node of an experiment's production graph (script, storyboard, each
    scene, assembly) with its status. The graph is derived deterministically
    from the genome; these rows are its persisted progress."""

    __tablename__ = "production_steps"
    __table_args__ = (UniqueConstraint("experiment_id", "key"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    key: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(20))
    position: Mapped[int]
    status: Mapped[StepStatus] = mapped_column(enum_column(StepStatus), default=StepStatus.PENDING)
    detail: Mapped[JSONDict] = mapped_column(default=dict)
    """Step inputs and outputs: scene plan, prompt, produced asset ids."""
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    experiment: Mapped[Experiment] = relationship()


class GenerationAttempt(Evidence, Identified, Base):
    """One paid (or blocked) call to a media provider. Failed attempts are kept
    so provider reliability, prompt failures, and wasted spend stay analysable.
    What was asked (prompt, request, model) never changes; only progress does."""

    __tablename__ = "generation_attempts"
    __mutable_columns__ = frozenset(
        {
            "status",
            "provider_job_id",
            "provider_response",
            "error",
            "actual_cost_usd",
            "started_at",
            "finished_at",
        }
    )
    __table_args__ = (UniqueConstraint("experiment_id", "attempt_number"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    attempt_number: Mapped[int]
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("production_steps.id"))
    strategy: Mapped[str] = mapped_column(String(40), default="original")
    """Why this attempt exists: original, prompt_repair, fallback_model, qa_repair."""
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(60))
    prompt: Mapped[str] = mapped_column(Text)
    request: Mapped[JSONDict]
    """The full provider-neutral request, for decision reproducibility."""
    status: Mapped[AttemptStatus] = mapped_column(
        enum_column(AttemptStatus), default=AttemptStatus.PENDING
    )
    provider_job_id: Mapped[str | None] = mapped_column(String(200))
    provider_response: Mapped[JSONDict | None]
    error: Mapped[str | None] = mapped_column(Text)
    estimated_cost_usd: Mapped[Money | None]
    actual_cost_usd: Mapped[Money | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    experiment: Mapped[Experiment] = relationship(back_populates="generation_attempts")


class Asset(Evidence, Identified, Base):
    """Metadata for a stored binary. The bytes live in object storage."""

    __tablename__ = "assets"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    generation_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id")
    )
    kind: Mapped[AssetKind] = mapped_column(enum_column(AssetKind))
    storage_uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(100))
    media_info: Mapped[JSONDict]
    """Probed facts: width, height, duration, codecs, audio presence."""

    experiment: Mapped[Experiment] = relationship(back_populates="assets")
    generation_attempt: Mapped[GenerationAttempt | None] = relationship()


class ProviderModel(Identified, Base):
    """Catalogue of generation models Hatch may route to, with the capability
    and price facts routing needs. Reference data, not evidence."""

    __tablename__ = "provider_models"
    __table_args__ = (UniqueConstraint("provider", "model", "operation"),)

    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(60))
    capabilities: Mapped[JSONDict]
    """Durations, resolutions, aspect ratios, native audio, ..."""
    pricing: Mapped[JSONDict]
    enabled: Mapped[bool] = mapped_column(default=True)
