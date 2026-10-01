from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.analytics.audit import analytics_completeness
from app.analytics.ingestion import ingest_publication, record_ingestion_failure
from app.platforms import Platform
from integrations.fake.analytics import FakeAnalyticsAdapter
from tests.analytics.test_ingestion import PUBLISHED_AT, publication_on, published_experiment


def observe(session: Session, publication, checkpoint: str) -> None:  # type: ignore[no-untyped-def]
    adapter = FakeAnalyticsAdapter(
        platform=publication.platform, payloads={publication.platform_post_id: {"views": 100}}
    )
    ingest_publication(session, publication.id, adapter, checkpoint=checkpoint, now=PUBLISHED_AT)


def test_every_due_observation_is_collected_failed_or_reported_missing(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    youtube = publication_on(session, experiment, Platform.YOUTUBE_SHORTS)
    tiktok = publication_on(session, experiment, Platform.TIKTOK)
    for checkpoint in ("1h", "6h", "24h"):
        observe(session, youtube, checkpoint)
    observe(session, tiktok, "1h")
    record_ingestion_failure(session, tiktok.id, checkpoint="6h", error="403 scope not granted")

    report = analytics_completeness(session, now=PUBLISHED_AT + timedelta(hours=30))

    assert report.posts == 4  # four platforms
    assert report.due == 4 * 3  # 1h, 6h and 24h are due; 72h is not yet
    assert report.collected == 4
    assert report.failed == 1
    assert report.missing == 7
    assert not report.complete
    gaps = {(gap.platform, gap.checkpoint) for gap in report.gaps}
    assert (Platform.TIKTOK, "24h") in gaps
    assert (Platform.TIKTOK, "6h") not in gaps  # an explicit failure is not a silent gap
    assert (Platform.YOUTUBE_SHORTS, "24h") not in gaps
    assert all(gap.experiment_id == experiment.id for gap in report.gaps)


def test_observations_inside_the_grace_period_are_not_yet_due(
    session: Session, tmp_path: Path
) -> None:
    published_experiment(session, tmp_path)

    report = analytics_completeness(
        session, now=PUBLISHED_AT + timedelta(hours=1, minutes=30), grace=timedelta(hours=2)
    )

    assert report.due == 0 and report.complete


def test_a_fully_observed_pilot_is_complete(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)
    for platform in Platform:
        observe(session, publication_on(session, experiment, platform), "1h")

    report = analytics_completeness(session, now=PUBLISHED_AT + timedelta(hours=4))

    assert (report.due, report.collected, report.missing) == (4, 4, 0)
    assert report.complete
