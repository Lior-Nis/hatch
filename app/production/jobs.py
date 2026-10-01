"""Queue jobs for media production."""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.experiments.states import VideoStatus
from app.production.run import ProductionDeps, produce_short
from app.quality.jobs import enqueue_qa
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError, RetryLater

PRODUCE_SHORT = "produce_short"

_STILL_WORKING = (VideoStatus.STORYBOARDED, VideoStatus.GENERATING)


def enqueue_production(
    session: Session, experiment_id: uuid.UUID, *, now: datetime | None = None
) -> JobRun:
    return enqueue(
        session,
        PRODUCE_SHORT,
        payload={},
        experiment_id=experiment_id,
        idempotency_key=f"{PRODUCE_SHORT}:{experiment_id}",
        now=now,
    )


def produce_short_handler(
    deps: ProductionDeps, *, poll_interval: timedelta = timedelta(seconds=10)
) -> JobHandler:
    """Start or resume one experiment's production without blocking the
    worker: each run checks the provider once and, if the video is still
    rendering, puts the job back to wait."""
    non_blocking = replace(deps, timeout_seconds=0.0)

    def handle(session: Session, job: JobRun) -> JobResult:
        if job.experiment_id is None:
            raise PermanentJobError("produce_short job has no experiment")
        result = produce_short(session, job.experiment_id, non_blocking)
        if result.video_status in _STILL_WORKING:
            raise RetryLater(poll_interval, "provider is still generating")
        if result.video_status is VideoStatus.GENERATED and result.asset_id is not None:
            # Follow-up work is due as of this run's clock (job.started_at).
            enqueue_qa(session, job.experiment_id, result.asset_id, now=job.started_at)
        return {
            "video_status": result.video_status.value,
            "asset_id": str(result.asset_id) if result.asset_id else None,
            "error": result.error,
        }

    return handle
