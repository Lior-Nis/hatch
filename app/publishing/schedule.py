"""Posting slots: when each IP's videos go out.

Each IP posts at fixed daily slots (two by default: the PRD's cadence of two
videos per IP per day). A video takes the first slot that is far enough in the
future and not already taken by another video of the same IP, so no two videos
of one brand share a posting time.
"""

import uuid
from datetime import UTC, datetime, time, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.publishing.models import Publication
from app.scheduling.models import JobRun


class PostingSchedule(BaseModel):
    model_config = ConfigDict(frozen=True)

    slots_utc: tuple[time, ...] = (time(15, 0), time(21, 0))
    lead: timedelta = timedelta(minutes=30)
    """Minimum notice before a slot, so the publisher has time to fetch media."""


def taken_slots(session: Session, ip_id: uuid.UUID, *, job_type: str) -> set[datetime]:
    """Posting times already used or reserved by this IP's videos."""
    published = session.scalars(
        select(Publication.scheduled_at)
        .join(Experiment, Experiment.id == Publication.experiment_id)
        .where(Experiment.ip_id == ip_id, Publication.scheduled_at.is_not(None))
    )
    queued = session.scalars(
        select(JobRun)
        .join(Experiment, Experiment.id == JobRun.experiment_id)
        .where(Experiment.ip_id == ip_id, JobRun.job_type == job_type)
    )
    taken = {moment for moment in published if moment is not None}
    for job in queued:
        if job.payload.get("scheduled_at"):
            taken.add(datetime.fromisoformat(job.payload["scheduled_at"]))
    return taken


def next_slot(
    session: Session,
    *,
    ip_id: uuid.UUID | None,
    now: datetime,
    schedule: PostingSchedule,
    taken: set[datetime] | None = None,
    job_type: str = "publish_video",
) -> datetime:
    """The first free posting slot for an IP at least ``schedule.lead`` away."""
    if taken is None:
        taken = taken_slots(session, ip_id, job_type=job_type) if ip_id else set()
    earliest = now + schedule.lead
    day = now.astimezone(UTC).date()
    while True:
        for slot in sorted(schedule.slots_utc):
            candidate = datetime.combine(day, slot, tzinfo=UTC)
            if candidate >= earliest and candidate not in taken:
                return candidate
        day += timedelta(days=1)
