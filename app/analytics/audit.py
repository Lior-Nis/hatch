"""Analytics completeness audit: is every observation that should exist either
collected or explicitly recorded as failed?

A published post owes one observation per maturity window once that window
(plus a grace period for the job to run) has passed. Anything due with neither
a snapshot nor a failure record is a silent gap — exactly what the pilot must
not have.
"""

import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.ingestion import CHECKPOINTS
from app.analytics.models import MetricIngestionFailure, MetricSnapshot
from app.db import utcnow
from app.platforms import Platform
from app.publishing.models import Publication, PublicationRecordStatus


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: uuid.UUID
    publication_id: uuid.UUID
    platform: Platform
    checkpoint: str
    due_at: datetime


class CompletenessReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    posts: int
    due: int
    collected: int
    failed: int
    missing: int
    gaps: list[Gap]

    @property
    def complete(self) -> bool:
        return self.missing == 0


def analytics_completeness(
    session: Session, *, now: datetime | None = None, grace: timedelta = timedelta(hours=2)
) -> CompletenessReport:
    now = now or utcnow()
    collected_keys = {
        (publication_id, checkpoint)
        for publication_id, checkpoint in session.execute(
            select(MetricSnapshot.publication_id, MetricSnapshot.checkpoint)
        )
    }
    failed_keys = {
        (publication_id, checkpoint)
        for publication_id, checkpoint in session.execute(
            select(MetricIngestionFailure.publication_id, MetricIngestionFailure.checkpoint)
        )
    }
    publications = session.scalars(
        select(Publication).where(
            Publication.status == PublicationRecordStatus.PUBLISHED,
            Publication.published_at.is_not(None),
        )
    ).all()
    due = collected = failed = 0
    gaps = []
    for publication in publications:
        assert publication.published_at is not None
        for checkpoint, offset in CHECKPOINTS.items():
            due_at = publication.published_at + offset
            if due_at + grace > now:
                continue
            due += 1
            key = (publication.id, checkpoint)
            if key in collected_keys:
                collected += 1
            elif key in failed_keys:
                failed += 1
            else:
                gaps.append(
                    Gap(
                        experiment_id=publication.experiment_id,
                        publication_id=publication.id,
                        platform=publication.platform,
                        checkpoint=checkpoint,
                        due_at=due_at,
                    )
                )
    return CompletenessReport(
        posts=len(publications),
        due=due,
        collected=collected,
        failed=failed,
        missing=len(gaps),
        gaps=gaps,
    )
