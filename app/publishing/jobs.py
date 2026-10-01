"""Queue jobs for publishing."""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.jobs import schedule_observations
from app.db import utcnow
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.platforms import Platform
from app.publishing.models import (
    AccountStatus,
    PlatformAccount,
    Publication,
    PublicationRecordStatus,
)
from app.publishing.ports import Publisher, PublisherError, PublishTargetError
from app.publishing.schedule import PostingSchedule, next_slot, taken_slots
from app.publishing.service import (
    PublishNotAllowed,
    plan_publications,
    refresh_publications,
    submit_publications,
)
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.recurring import ensure_recurring
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError, RetryLater
from app.storage import AssetNotPublic, AssetStore

logger = logging.getLogger(__name__)

PUBLISH_VIDEO = "publish_video"
REFRESH_PUBLICATIONS = "refresh_publications"
PUBLISHING_CYCLE = "publishing_cycle"

# How long after the posting time the first status check happens.
_FIRST_CHECK_AFTER = timedelta(minutes=2)


def schedule_ready_videos(
    session: Session, *, now: datetime | None = None, schedule: PostingSchedule | None = None
) -> list[JobRun]:
    """Give every approved, not-yet-scheduled video the next free posting slot
    of its IP and queue its publication. Returns only the newly queued jobs.

    Videos of an IP that does not have an active account on all four platforms
    are left waiting (and logged) rather than queued to fail."""
    now = now or utcnow()
    schedule = schedule or PostingSchedule()
    ready = session.scalars(
        select(Experiment)
        .where(Experiment.video_status == VideoStatus.READY)
        .order_by(Experiment.created_at)
    ).all()
    taken_by_ip: dict[uuid.UUID, set[datetime]] = {}
    queued = []
    for experiment in ready:
        key = f"{PUBLISH_VIDEO}:{experiment.id}"
        already = session.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.idempotency_key == key)
        )
        has_publications = session.scalar(
            select(func.count())
            .select_from(Publication)
            .where(Publication.experiment_id == experiment.id)
        )
        if already or has_publications:
            continue
        accounts = set(
            session.scalars(
                select(PlatformAccount.platform).where(
                    PlatformAccount.ip_id == experiment.ip_id,
                    PlatformAccount.status == AccountStatus.ACTIVE,
                )
            )
        )
        if accounts != set(Platform):
            logger.info(
                "publish_waiting_for_accounts",
                extra={"experiment_id": str(experiment.id), "ip": experiment.ip.slug},
            )
            continue
        taken = taken_by_ip.setdefault(
            experiment.ip_id, taken_slots(session, experiment.ip_id, job_type=PUBLISH_VIDEO)
        )
        slot = next_slot(session, ip_id=experiment.ip_id, now=now, schedule=schedule, taken=taken)
        taken.add(slot)
        queued.append(
            enqueue(
                session,
                PUBLISH_VIDEO,
                payload={"scheduled_at": slot.isoformat()},
                experiment_id=experiment.id,
                idempotency_key=key,
                max_attempts=5,
                now=now,
            )
        )
    session.commit()
    return queued


def publishing_handlers(
    *,
    publisher: Publisher,
    store: AssetStore,
    poll_interval: timedelta = timedelta(minutes=15),
    schedule: PostingSchedule | None = None,
    cycle_interval: timedelta = timedelta(minutes=30),
) -> dict[str, JobHandler]:
    def publish(session: Session, job: JobRun) -> JobResult:
        if job.experiment_id is None:
            raise PermanentJobError("publish_video job has no experiment")
        scheduled_at = datetime.fromisoformat(job.payload["scheduled_at"])
        try:
            plan_publications(session, job.experiment_id, scheduled_at=scheduled_at)
            publications = submit_publications(
                session, job.experiment_id, publisher=publisher, store=store
            )
        except (PublishNotAllowed, PublishTargetError, AssetNotPublic) as exc:
            raise PermanentJobError(str(exc)) from exc
        except PublisherError as exc:
            if not exc.retryable:
                raise PermanentJobError(str(exc)) from exc
            raise  # retried with backoff; the next run only submits what is missing
        enqueue(
            session,
            REFRESH_PUBLICATIONS,
            payload={},
            experiment_id=job.experiment_id,
            idempotency_key=f"{REFRESH_PUBLICATIONS}:{job.experiment_id}",
            run_at=scheduled_at + _FIRST_CHECK_AFTER,
            max_attempts=5,
            now=job.started_at,
        )
        return {"publications": {p.platform.value: p.status.value for p in publications}}

    def refresh(session: Session, job: JobRun) -> JobResult:
        if job.experiment_id is None:
            raise PermanentJobError("refresh_publications job has no experiment")
        publications = refresh_publications(session, job.experiment_id, publisher=publisher)
        for publication in publications:
            if publication.status is PublicationRecordStatus.PUBLISHED:
                # Start measuring: one observation per maturity window.
                schedule_observations(session, publication, now=job.started_at)
        if any(p.status is PublicationRecordStatus.SCHEDULED for p in publications):
            raise RetryLater(poll_interval, "waiting for platforms to publish")
        return {"publications": {p.platform.value: p.status.value for p in publications}}

    def cycle(session: Session, job: JobRun) -> JobResult:
        """Give newly approved videos their posting slots, then make sure the
        next cycle exists."""
        now = job.started_at
        assert now is not None
        queued = schedule_ready_videos(session, now=now, schedule=schedule)
        ensure_recurring(
            session, PUBLISHING_CYCLE, interval=cycle_interval, now=now + cycle_interval
        )
        return {"queued": len(queued)}

    return {PUBLISH_VIDEO: publish, REFRESH_PUBLICATIONS: refresh, PUBLISHING_CYCLE: cycle}
