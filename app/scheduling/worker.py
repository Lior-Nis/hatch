"""Job worker: claims queued jobs and runs their handlers.

Retry rules:
- ``RetryLater``        → wait and run again; not a failure, no attempt used.
- ``PermanentJobError`` → fail now; retrying cannot help.
- programming/state errors (``ValueError``, ``IllegalTransition``, ...) → fail now.
- anything else (provider outages, network) → retry with backoff until
  ``max_attempts``, then FAILED and visible to the operator.

A handler's uncommitted work is rolled back when it raises. Handlers must be
idempotent: a job may run more than once (crash, retry, replay) and must not
duplicate media, charges, or publications.
"""

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db import utcnow
from app.lifecycle import IllegalTransition
from app.scheduling.models import JobRun
from app.scheduling.queue import claim_next, complete, fail, requeue_stale, reschedule

logger = logging.getLogger(__name__)

JobResult = dict[str, Any] | None
JobHandler = Callable[[Session, JobRun], JobResult]
SessionFactory = Callable[[], AbstractContextManager[Session]]

_NON_RETRYABLE = (ValueError, TypeError, KeyError, LookupError, IllegalTransition)


class RetryLater(Exception):
    """The work is not finished yet; run this job again after ``delay``."""

    def __init__(self, delay: timedelta, reason: str = "") -> None:
        self.delay = delay
        super().__init__(reason or f"retry in {delay}")


class PermanentJobError(Exception):
    """The job can never succeed; do not retry."""


class Worker:
    def __init__(
        self,
        sessions: SessionFactory,
        handlers: Mapping[str, JobHandler],
        *,
        worker_id: str,
        clock: Callable[[], datetime] = utcnow,
        lock_timeout: timedelta = timedelta(minutes=30),
    ) -> None:
        self._sessions = sessions
        self._handlers = handlers
        self._worker_id = worker_id
        self._clock = clock
        self._lock_timeout = lock_timeout

    def run_once(self) -> bool:
        """Run at most one due job. Returns False when the queue is idle."""
        with self._sessions() as session:
            requeue_stale(session, lock_timeout=self._lock_timeout, now=self._clock())
            job = claim_next(session, worker_id=self._worker_id, now=self._clock())
            if job is None:
                return False
            self._run(session, job)
            return True

    def run_forever(self, *, poll_interval_seconds: float = 2.0) -> None:
        while True:
            if not self.run_once():
                time.sleep(poll_interval_seconds)

    def _run(self, session: Session, job: JobRun) -> None:
        job_id: uuid.UUID = job.id
        context = {
            "job_id": str(job_id),
            "job_type": job.job_type,
            "experiment_id": str(job.experiment_id) if job.experiment_id else None,
        }
        handler = self._handlers.get(job.job_type)
        if handler is None:
            error = f"no handler registered for job type {job.job_type!r}"
            fail(session, job, error=error, now=self._clock(), retryable=False)
            logger.error("job_failed", extra={**context, "error": error})
            return
        try:
            result = handler(session, job)
        except RetryLater as wait:
            session.rollback()
            reschedule(
                session, session.get_one(JobRun, job_id), delay=wait.delay, now=self._clock()
            )
            logger.info("job_waiting", extra={**context, "reason": str(wait)})
        except Exception as exc:
            session.rollback()
            retryable = not isinstance(exc, (PermanentJobError, *_NON_RETRYABLE))
            fail(
                session,
                session.get_one(JobRun, job_id),
                error=str(exc) or type(exc).__name__,
                now=self._clock(),
                retryable=retryable,
            )
            logger.exception("job_failed", extra={**context, "retryable": retryable})
        else:
            complete(session, session.get_one(JobRun, job_id), result=result, now=self._clock())
            logger.info("job_succeeded", extra=context)
