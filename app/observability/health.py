"""Operational health: provider reliability and pipeline stalls."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.models import AttemptStatus, GenerationAttempt
from app.scheduling.models import JobRun, JobStatus

# States the system should move through on its own. A video sitting in one of
# these for long means a job died or was never queued. Waiting for a human
# (APPROVAL_PENDING) or for a schedule is not a stall.
_AUTOMATED_STATES = (
    VideoStatus.SCRIPTED,
    VideoStatus.STORYBOARDED,
    VideoStatus.GENERATING,
    VideoStatus.GENERATED,
    VideoStatus.QA_PENDING,
)


class ProviderHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    attempts: int
    """Finished attempts (succeeded + failed)."""
    succeeded: int
    failed: int
    in_flight: int
    blocked_by_budget: int
    failure_rate: float | None
    failed_cost_usd: Decimal
    last_error: str | None


class Stall(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    since: datetime
    detail: str
    experiment_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


def provider_health(session: Session, *, since: datetime | None = None) -> list[ProviderHealth]:
    query = select(GenerationAttempt).order_by(GenerationAttempt.created_at)
    if since is not None:
        query = query.where(GenerationAttempt.created_at >= since)
    grouped: dict[tuple[str, str], list[GenerationAttempt]] = {}
    for attempt in session.scalars(query):
        grouped.setdefault((attempt.provider, attempt.model), []).append(attempt)

    report = []
    for (provider, model), attempts in sorted(grouped.items()):
        succeeded = [a for a in attempts if a.status is AttemptStatus.SUCCEEDED]
        failed = [a for a in attempts if a.status is AttemptStatus.FAILED]
        finished = len(succeeded) + len(failed)
        report.append(
            ProviderHealth(
                provider=provider,
                model=model,
                attempts=finished,
                succeeded=len(succeeded),
                failed=len(failed),
                in_flight=sum(
                    a.status in (AttemptStatus.PENDING, AttemptStatus.RUNNING) for a in attempts
                ),
                blocked_by_budget=sum(a.status is AttemptStatus.BLOCKED_BUDGET for a in attempts),
                failure_rate=len(failed) / finished if finished else None,
                failed_cost_usd=sum(
                    (a.actual_cost_usd or Decimal("0") for a in failed), Decimal("0")
                ),
                last_error=failed[-1].error if failed else None,
            )
        )
    return report


def find_stalls(
    session: Session,
    *,
    now: datetime | None = None,
    stall_after: timedelta = timedelta(minutes=30),
    job_lock_timeout: timedelta = timedelta(minutes=30),
    queue_overdue_after: timedelta = timedelta(minutes=15),
) -> list[Stall]:
    now = now or utcnow()
    stalls: list[Stall] = []

    for experiment in session.scalars(
        select(Experiment).where(
            Experiment.video_status.in_(_AUTOMATED_STATES),
            Experiment.video_status_changed_at < now - stall_after,
        )
    ):
        stalls.append(
            Stall(
                kind="video_stalled",
                since=experiment.video_status_changed_at,
                experiment_id=experiment.id,
                detail=f"video has been {experiment.video_status.value} with no progress",
            )
        )

    for job in session.scalars(
        select(JobRun).where(
            JobRun.status == JobStatus.RUNNING, JobRun.locked_at < now - job_lock_timeout
        )
    ):
        assert job.locked_at is not None
        stalls.append(
            Stall(
                kind="job_stuck",
                since=job.locked_at,
                job_id=job.id,
                experiment_id=job.experiment_id,
                detail=f"{job.job_type} has been running on {job.locked_by} past the lock timeout",
            )
        )

    for job in session.scalars(
        select(JobRun).where(
            JobRun.status == JobStatus.QUEUED, JobRun.run_at < now - queue_overdue_after
        )
    ):
        stalls.append(
            Stall(
                kind="queue_not_draining",
                since=job.run_at,
                job_id=job.id,
                experiment_id=job.experiment_id,
                detail=f"{job.job_type} is overdue: is a worker running?",
            )
        )
    return stalls
