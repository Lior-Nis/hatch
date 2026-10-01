"""Durable job queue on Postgres.

A job is a ``job_runs`` row, so queued work survives process restarts and is
visible to the operator. Workers claim rows with ``FOR UPDATE SKIP LOCKED``,
which lets several workers run without ever taking the same job. A job left
RUNNING by a crashed worker is returned to the queue after a lock timeout.

Enqueueing is idempotent on ``idempotency_key``: asking twice for the same
piece of work yields one job.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.scheduling.models import JobRun, JobStatus

_BASE_BACKOFF = timedelta(seconds=30)
_MAX_BACKOFF = timedelta(hours=1)


def backoff(attempts: int) -> timedelta:
    """Delay before retry number ``attempts + 1``: 30s, 1m, 2m, ... capped at 1h."""
    delay: timedelta = _BASE_BACKOFF * int(2 ** max(attempts - 1, 0))
    return min(delay, _MAX_BACKOFF)


def enqueue(
    session: Session,
    job_type: str,
    *,
    payload: dict[str, Any],
    experiment_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    run_at: datetime | None = None,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> JobRun:
    if idempotency_key is not None:
        existing = session.scalars(
            select(JobRun).where(JobRun.idempotency_key == idempotency_key)
        ).one_or_none()
        if existing is not None:
            return existing
    now = now or utcnow()
    job = JobRun(
        job_type=job_type,
        payload=payload,
        experiment_id=experiment_id,
        idempotency_key=idempotency_key,
        run_at=run_at or now,
        max_attempts=max_attempts,
        created_at=now,
    )
    session.add(job)
    session.flush()
    return job


def claim_next(
    session: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
    job_types: Sequence[str] | None = None,
) -> JobRun | None:
    """Take the oldest due job, mark it RUNNING, and commit the claim."""
    now = now or utcnow()
    query = (
        select(JobRun)
        .where(JobRun.status == JobStatus.QUEUED, JobRun.run_at <= now)
        .order_by(JobRun.run_at, JobRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_types is not None:
        query = query.where(JobRun.job_type.in_(job_types))
    job = session.scalars(query).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_by = worker_id
    job.locked_at = now
    job.started_at = now
    session.commit()
    return job


def complete(
    session: Session, job: JobRun, *, result: dict[str, Any] | None, now: datetime | None = None
) -> None:
    job.status = JobStatus.SUCCEEDED
    job.result = result
    job.error = None
    job.finished_at = now or utcnow()
    _unlock(job)
    session.commit()


def fail(
    session: Session,
    job: JobRun,
    *,
    error: str,
    now: datetime | None = None,
    retryable: bool = True,
) -> None:
    """Record a failure. Retryable failures go back on the queue with backoff
    until ``max_attempts`` is reached; then the job is FAILED for the operator."""
    now = now or utcnow()
    job.error = error
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.run_at = now + backoff(job.attempts)
    else:
        job.status = JobStatus.FAILED
        job.finished_at = now
    _unlock(job)
    session.commit()


def reschedule(
    session: Session, job: JobRun, *, delay: timedelta, now: datetime | None = None
) -> None:
    """Put a running job back to wait (e.g. the provider is still rendering).
    Waiting is not failing, so the attempt is not counted."""
    job.status = JobStatus.QUEUED
    job.attempts = max(job.attempts - 1, 0)
    job.run_at = (now or utcnow()) + delay
    _unlock(job)
    session.commit()


def requeue_stale(session: Session, *, lock_timeout: timedelta, now: datetime | None = None) -> int:
    """Return jobs abandoned by a dead worker to the queue. Their attempt
    stays counted, so a job that keeps killing its worker eventually fails."""
    now = now or utcnow()
    result = session.execute(
        update(JobRun)
        .where(JobRun.status == JobStatus.RUNNING, JobRun.locked_at < now - lock_timeout)
        .values(status=JobStatus.QUEUED, locked_by=None, locked_at=None, run_at=now)
        .execution_options(synchronize_session="fetch")
    )
    session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


def _unlock(job: JobRun) -> None:
    job.locked_by = None
    job.locked_at = None
