import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, JSONDict, enum_column, utcnow
from app.experiments.models import Experiment


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobRun(Identified, Base):
    """A durable background job. The row *is* the queue entry: it survives
    process restarts and shows status, attempts, and timing to the operator."""

    __tablename__ = "job_runs"

    job_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.QUEUED, index=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[JSONDict]
    result: Mapped[JSONDict | None]
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    error: Mapped[str | None] = mapped_column(Text)
    run_at: Mapped[datetime] = mapped_column(default=utcnow)
    """Earliest time the job may run (used for scheduling and retry backoff)."""
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    experiment: Mapped[Experiment | None] = relationship(back_populates="job_runs")
