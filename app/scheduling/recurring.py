"""Recurring jobs on top of the durable queue.

Time is cut into fixed windows; each window has exactly one job, keyed by its
start time. Asking again inside the same window returns the existing job, so
several workers (or restarts) cannot double-schedule a cycle.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def window_start(now: datetime, interval: timedelta) -> datetime:
    return _EPOCH + ((now - _EPOCH) // interval) * interval


def ensure_recurring(
    session: Session,
    job_type: str,
    *,
    interval: timedelta,
    now: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> JobRun:
    """The job for the window containing ``now`` (created if missing)."""
    now = now or utcnow()
    start = window_start(now, interval)
    return enqueue(
        session,
        job_type,
        payload={**(payload or {}), "interval_seconds": interval.total_seconds()},
        idempotency_key=f"{job_type}:{start.isoformat()}",
        run_at=start,
        now=now,
    )
