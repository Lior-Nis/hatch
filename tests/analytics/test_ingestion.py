from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.ingestion import (
    CHECKPOINTS,
    IngestionNotAllowed,
    find_snapshots,
    ingest_publication,
)
from app.analytics.jobs import INGEST_METRICS, analytics_handlers, schedule_observations
from app.analytics.models import MetricIngestionFailure, MetricSnapshot
from app.analytics.ports import AnalyticsError
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.platforms import Platform
from app.publishing.models import Publication, PublicationRecordStatus
from app.publishing.service import plan_publications, refresh_publications, submit_publications
from app.quality.models import ReviewDecision
from app.quality.review import submit_review
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.worker import Worker
from integrations.fake.analytics import FakeAnalyticsAdapter
from integrations.fake.publisher import FakePublisher
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_reviewable_experiment
from tests.publishing.test_service import CHANNELS, map_all

PUBLISHED_AT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = PUBLISHED_AT

    def __call__(self) -> datetime:
        return self.now


def published_experiment(session: Session, tmp_path: Path) -> Experiment:
    """An approved video published on all four platforms at PUBLISHED_AT."""
    store = LocalAssetStore(tmp_path / "assets", public_base_url="https://media.example.com")
    experiment = make_reviewable_experiment(session, tmp_path, store=store)
    submit_review(
        session, experiment.id, decision=ReviewDecision.APPROVE, reason="ok", reviewer="lior"
    )
    map_all(session, experiment.ip)
    publisher = FakePublisher(accounts=set(CHANNELS.values()))
    plan_publications(session, experiment.id, scheduled_at=PUBLISHED_AT)
    submit_publications(session, experiment.id, publisher=publisher, store=store)
    for publication in session.scalars(select(Publication)):
        if publication.experiment_id == experiment.id:
            publisher.deliver(publication.external_id or "")
    refresh_publications(session, experiment.id, publisher=publisher)
    for publication in session.scalars(
        select(Publication).where(Publication.experiment_id == experiment.id)
    ):
        publication.published_at = PUBLISHED_AT
    session.flush()
    session.expire_all()
    return session.get_one(Experiment, experiment.id)


def publication_on(session: Session, experiment: Experiment, platform: Platform) -> Publication:
    return session.scalars(
        select(Publication).where(
            Publication.experiment_id == experiment.id, Publication.platform == platform
        )
    ).one()


def adapter(platform: Platform, post_id: str, **payload: Any) -> FakeAnalyticsAdapter:
    raw = {"play_count": 1200, "full_video_watched_rate": 0.41, **payload}
    return FakeAnalyticsAdapter(platform=platform, payloads={post_id: raw})


# --- snapshots -----------------------------------------------------------------


def test_snapshot_keeps_raw_and_normalized_values_side_by_side(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    source = adapter(Platform.TIKTOK, publication.platform_post_id or "")

    snapshot = ingest_publication(
        session,
        publication.id,
        source,
        checkpoint="24h",
        now=PUBLISHED_AT + timedelta(hours=24, minutes=5),
    )

    session.expire_all()
    stored = session.get_one(MetricSnapshot, snapshot.id)
    assert stored.raw == {"play_count": 1200, "full_video_watched_rate": 0.41}
    assert stored.normalized["views"] == 1200
    assert stored.normalized["completion_rate"] == 0.41
    assert stored.normalized["likes"] is None  # not reported is not zero
    assert stored.checkpoint == "24h"
    assert stored.hours_since_publication == pytest.approx(24.08, abs=0.01)
    assert (stored.adapter, stored.adapter_version) == ("fake", "fake-1")
    assert stored.publication.experiment_id == experiment.id


def test_both_raw_and_normalized_values_can_be_queried(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    source = adapter(Platform.TIKTOK, publication.platform_post_id or "")
    ingest_publication(session, publication.id, source, checkpoint="24h", now=PUBLISHED_AT)

    by_normalized = find_snapshots(session, normalized={"views": 1200})
    by_raw = find_snapshots(session, raw={"play_count": 1200})
    none = find_snapshots(session, raw={"play_count": 7})

    assert [s.publication_id for s in by_normalized] == [publication.id]
    assert [s.id for s in by_raw] == [s.id for s in by_normalized]
    assert none == []


def test_later_observations_are_new_rows_and_earlier_ones_are_never_changed(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    post_id = publication.platform_post_id or ""
    early = adapter(Platform.TIKTOK, post_id)
    late = adapter(Platform.TIKTOK, post_id, play_count=9000)

    ingest_publication(session, publication.id, early, checkpoint="24h", now=PUBLISHED_AT)
    ingest_publication(session, publication.id, late, checkpoint="72h", now=PUBLISHED_AT)

    views = [s.normalized["views"] for s in publication.metric_snapshots]
    assert sorted(views) == [1200, 9000]


def test_metrics_cannot_be_ingested_for_a_post_that_is_not_published(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    publication.status = PublicationRecordStatus.FAILED
    session.flush()

    with pytest.raises(IngestionNotAllowed):
        ingest_publication(
            session,
            publication.id,
            adapter(Platform.TIKTOK, publication.platform_post_id or ""),
            checkpoint="24h",
            now=PUBLISHED_AT,
        )


def test_an_adapter_for_the_wrong_platform_is_refused(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)

    with pytest.raises(IngestionNotAllowed, match="platform"):
        ingest_publication(
            session,
            publication.id,
            adapter(Platform.YOUTUBE_SHORTS, publication.platform_post_id or ""),
            checkpoint="24h",
            now=PUBLISHED_AT,
        )


# --- observation schedule ------------------------------------------------------


def test_checkpoints_are_the_agreed_maturity_windows() -> None:
    assert list(CHECKPOINTS) == ["1h", "6h", "24h", "72h", "7d", "30d"]
    assert CHECKPOINTS["72h"] == timedelta(hours=72)


def test_each_published_post_gets_one_observation_job_per_window(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.YOUTUBE_SHORTS)

    jobs = schedule_observations(session, publication)
    schedule_observations(session, publication)  # idempotent

    stored = session.scalars(select(JobRun).where(JobRun.job_type == INGEST_METRICS)).all()
    assert len(stored) == len(jobs) == 6
    due = {job.payload["checkpoint"]: job.run_at - PUBLISHED_AT for job in stored}
    assert due == dict(CHECKPOINTS)
    assert {job.experiment_id for job in stored} == {experiment.id}


def run_due_jobs(session: Session, adapters: dict[Platform, Any], clock: Clock) -> None:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    worker = Worker(sessions, analytics_handlers(adapters), worker_id="w1", clock=clock)
    for _ in range(100):
        if not worker.run_once():
            return


def test_a_published_short_gains_linked_observations_over_time(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    schedule_observations(session, publication)
    adapters = {Platform.TIKTOK: adapter(Platform.TIKTOK, publication.platform_post_id or "")}
    clock = Clock()

    clock.now = PUBLISHED_AT + timedelta(hours=7)
    run_due_jobs(session, adapters, clock)
    assert [s.checkpoint for s in publication.metric_snapshots] == ["1h", "6h"]

    clock.now = PUBLISHED_AT + timedelta(days=31)
    run_due_jobs(session, adapters, clock)
    session.expire_all()
    assert [s.checkpoint for s in publication.metric_snapshots] == [
        "1h", "6h", "24h", "72h", "7d", "30d",
    ]  # fmt: skip
    assert all(s.publication_id == publication.id for s in publication.metric_snapshots)


def test_first_observation_moves_the_video_to_observing(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)

    ingest_publication(
        session,
        publication.id,
        adapter(Platform.TIKTOK, publication.platform_post_id or ""),
        checkpoint="1h",
        now=PUBLISHED_AT + timedelta(hours=1),
    )

    session.expire_all()
    refreshed = session.get_one(Experiment, experiment.id)
    assert refreshed.video_status is VideoStatus.OBSERVING
    assert refreshed.status is ExperimentStatus.OBSERVING


def test_an_analytics_outage_is_retried_then_recorded_as_an_explicit_failure(
    session: Session, tmp_path: Path
) -> None:
    class Down:
        platform = Platform.TIKTOK
        name = "fake"
        version = "fake-1"

        def fetch_post_metrics(self, **_: Any) -> Any:
            raise AnalyticsError("403: scope not granted", retryable=False)

    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    schedule_observations(session, publication)
    clock = Clock()
    clock.now = PUBLISHED_AT + timedelta(hours=2)

    run_due_jobs(session, {Platform.TIKTOK: Down()}, clock)

    failure = session.scalars(select(MetricIngestionFailure)).one()
    assert (failure.publication_id, failure.checkpoint) == (publication.id, "1h")
    assert "scope not granted" in failure.error
    job = session.scalars(select(JobRun).where(JobRun.payload["checkpoint"].astext == "1h")).one()
    assert job.status is JobStatus.FAILED
    assert publication.metric_snapshots == []


def test_a_platform_without_a_configured_adapter_fails_visibly(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.FACEBOOK_REELS)
    schedule_observations(session, publication)
    clock = Clock()
    clock.now = PUBLISHED_AT + timedelta(hours=2)

    run_due_jobs(session, {}, clock)

    failure = session.scalars(select(MetricIngestionFailure)).one()
    assert "no analytics adapter" in failure.error
