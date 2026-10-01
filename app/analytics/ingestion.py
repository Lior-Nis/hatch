"""Metric ingestion: platform observations → immutable snapshots.

Every snapshot belongs to exactly one publication (and through it to one
experiment and one platform account), stores the raw platform payload next to
Hatch's normalized view, and is never rewritten: a later observation is a new
row. Signals a platform does not report stay ``None`` — never zero.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.models import MetricIngestionFailure, MetricSnapshot
from app.analytics.ports import AnalyticsAdapter
from app.db import utcnow
from app.experiments.states import VideoStatus
from app.publishing.models import Publication, PublicationRecordStatus

# Maturity windows after publication. 72h is the main early-evaluation
# checkpoint; later observations keep updating the evidence.
CHECKPOINTS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "72h": timedelta(hours=72),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
EARLY_EVALUATION_CHECKPOINT = "72h"
FINAL_CHECKPOINT = "30d"


class IngestionNotAllowed(Exception):
    """The publication cannot have metrics attributed to it."""


def ingest_publication(
    session: Session,
    publication_id: uuid.UUID,
    adapter: AnalyticsAdapter,
    *,
    checkpoint: str | None,
    now: datetime | None = None,
) -> MetricSnapshot:
    """Fetch one publication's current metrics and store them as a snapshot.
    Raises ``AnalyticsError`` from the adapter and ``IngestionNotAllowed``."""
    now = now or utcnow()
    publication = session.get_one(Publication, publication_id)
    if publication.status is not PublicationRecordStatus.PUBLISHED:
        raise IngestionNotAllowed(
            f"publication {publication_id} is {publication.status.value}, not published"
        )
    if publication.platform_post_id is None or publication.platform_account is None:
        raise IngestionNotAllowed(
            f"publication {publication_id} has no platform post id or account to attribute to"
        )
    if adapter.platform is not publication.platform:
        raise IngestionNotAllowed(
            f"adapter for platform {adapter.platform.value} cannot read a "
            f"{publication.platform.value} publication"
        )

    observation = adapter.fetch_post_metrics(
        platform_account_id=publication.platform_account.external_account_id,
        platform_post_id=publication.platform_post_id,
    )
    published_at = publication.published_at or now
    snapshot = MetricSnapshot(
        publication=publication,
        observed_at=observation.observed_at,
        hours_since_publication=(now - published_at).total_seconds() / 3600,
        checkpoint=checkpoint,
        adapter=adapter.name,
        adapter_version=adapter.version,
        raw=dict(observation.raw),
        normalized=observation.normalized.model_dump(mode="json"),
    )
    session.add(snapshot)
    experiment = publication.experiment
    if experiment.video_status is VideoStatus.PUBLISHED:
        experiment.video_status = VideoStatus.OBSERVING
    session.flush()
    return snapshot


def record_ingestion_failure(
    session: Session, publication_id: uuid.UUID, *, checkpoint: str | None, error: str
) -> MetricIngestionFailure:
    failure = MetricIngestionFailure(
        publication_id=publication_id, checkpoint=checkpoint, error=error
    )
    session.add(failure)
    session.flush()
    return failure


def find_snapshots(
    session: Session,
    *,
    normalized: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
) -> list[MetricSnapshot]:
    """Query snapshots by normalized values, raw platform values, or both."""
    query = select(MetricSnapshot).order_by(MetricSnapshot.observed_at)
    if normalized:
        query = query.where(MetricSnapshot.normalized.contains(normalized))
    if raw:
        query = query.where(MetricSnapshot.raw.contains(raw))
    return list(session.scalars(query))
