"""Queue jobs for analytics ingestion at the agreed maturity windows."""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy.orm import Session

from app.analytics.ingestion import (
    CHECKPOINTS,
    EVALUATION_CHECKPOINTS,
    IngestionNotAllowed,
    ingest_publication,
    record_ingestion_failure,
)
from app.analytics.ports import AnalyticsAdapter, AnalyticsError
from app.db import utcnow
from app.fitness.jobs import enqueue_fitness_evaluation
from app.platforms import Platform
from app.publishing.models import Publication
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError

INGEST_METRICS = "ingest_metrics"


def schedule_observations(
    session: Session, publication: Publication, *, now: datetime | None = None
) -> list[JobRun]:
    """Queue one observation per maturity window for a published post."""
    published_at = publication.published_at or now or utcnow()
    return [
        enqueue(
            session,
            INGEST_METRICS,
            payload={"publication_id": str(publication.id), "checkpoint": checkpoint},
            experiment_id=publication.experiment_id,
            idempotency_key=f"{INGEST_METRICS}:{publication.id}:{checkpoint}",
            run_at=published_at + offset,
            max_attempts=4,
            now=now,
        )
        for checkpoint, offset in CHECKPOINTS.items()
    ]


def analytics_handlers(adapters: Mapping[Platform, AnalyticsAdapter]) -> dict[str, JobHandler]:
    def ingest(session: Session, job: JobRun) -> JobResult:
        publication = session.get_one(Publication, job.payload["publication_id"])
        checkpoint = job.payload["checkpoint"]

        def give_up(error: str) -> PermanentJobError:
            # Committed before raising: a failed observation must leave a record.
            record_ingestion_failure(session, publication.id, checkpoint=checkpoint, error=error)
            session.commit()
            return PermanentJobError(error)

        adapter = adapters.get(publication.platform)
        if adapter is None:
            raise give_up(f"no analytics adapter is configured for {publication.platform.value}")
        try:
            snapshot = ingest_publication(
                session, publication.id, adapter, checkpoint=checkpoint, now=job.started_at
            )
        except IngestionNotAllowed as exc:
            raise give_up(str(exc)) from exc
        except AnalyticsError as exc:
            if exc.retryable and job.attempts < job.max_attempts:
                raise
            raise give_up(str(exc)) from exc
        if checkpoint in EVALUATION_CHECKPOINTS:
            enqueue_fitness_evaluation(session, snapshot, now=job.started_at)
        return {"snapshot_id": str(snapshot.id), "checkpoint": checkpoint}

    return {INGEST_METRICS: ingest}
