from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.platforms import Platform
from app.publishing.jobs import (
    PUBLISH_VIDEO,
    REFRESH_PUBLICATIONS,
    publishing_handlers,
    schedule_ready_videos,
)
from app.publishing.models import Publication, PublicationRecordStatus
from app.publishing.ports import PublisherError
from app.publishing.schedule import PostingSchedule, next_slot
from app.quality.models import ReviewDecision
from app.quality.review import submit_review
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.worker import Worker
from integrations.fake.publisher import FakePublisher
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_reviewable_experiment
from tests.publishing.test_service import CHANNELS, map_all

SCHEDULE = PostingSchedule(slots_utc=(time(15, 0), time(21, 0)), lead=timedelta(minutes=30))


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 10, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path: Path) -> LocalAssetStore:
    return LocalAssetStore(tmp_path / "assets", public_base_url="https://media.example.com")


def approved(session: Session, tmp_path: Path, store: LocalAssetStore) -> Experiment:
    experiment = make_reviewable_experiment(session, tmp_path, store=store)
    submit_review(
        session, experiment.id, decision=ReviewDecision.APPROVE, reason="ok", reviewer="lior"
    )
    return experiment


def worker(
    session: Session, publisher: FakePublisher, store: LocalAssetStore, clock: Clock
) -> Worker:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    return Worker(
        sessions,
        publishing_handlers(publisher=publisher, store=store, poll_interval=timedelta(minutes=15)),
        worker_id="w1",
        clock=clock,
    )


def status_of(session: Session, job: JobRun) -> JobStatus:
    return session.get_one(JobRun, job.id).status


def drain(job_worker: Worker) -> None:
    for _ in range(20):
        if not job_worker.run_once():
            return
    raise AssertionError("queue did not drain")


# --- posting slots ---------------------------------------------------------------


def test_next_slot_is_the_first_free_slot_after_the_lead_time(session: Session) -> None:
    now = datetime(2026, 10, 1, 14, 45, tzinfo=UTC)  # 15:00 is inside the 30-minute lead

    slot = next_slot(session, ip_id=None, now=now, schedule=SCHEDULE)

    assert slot == datetime(2026, 10, 1, 21, 0, tzinfo=UTC)


def test_slots_roll_over_to_the_next_day(session: Session) -> None:
    now = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)

    assert next_slot(session, ip_id=None, now=now, schedule=SCHEDULE) == datetime(
        2026, 10, 2, 15, 0, tzinfo=UTC
    )


def test_two_videos_of_one_ip_never_share_a_slot(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    first, second, third = (approved(session, tmp_path, store) for _ in range(3))
    map_all(session, first.ip)

    jobs = schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)

    slots = sorted(job.payload["scheduled_at"] for job in jobs)
    assert slots == [
        "2026-10-01T15:00:00+00:00",
        "2026-10-01T21:00:00+00:00",
        "2026-10-02T15:00:00+00:00",
    ]
    assert {job.experiment_id for job in jobs} == {first.id, second.id, third.id}


def test_scheduling_ready_videos_twice_queues_each_video_once(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    experiment = approved(session, tmp_path, store)
    map_all(session, experiment.ip)

    schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)
    again = schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)

    assert again == []
    assert len(session.scalars(select(JobRun)).all()) == 1


def test_videos_of_an_ip_without_all_four_accounts_are_left_waiting(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    approved(session, tmp_path, store)  # no accounts mapped

    assert schedule_ready_videos(session, now=clock(), schedule=SCHEDULE) == []


# --- jobs ------------------------------------------------------------------------


def test_publish_job_schedules_all_four_platforms_then_tracks_them(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    experiment = approved(session, tmp_path, store)
    map_all(session, experiment.ip)
    publisher = FakePublisher(accounts=set(CHANNELS.values()))
    schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)
    job_worker = worker(session, publisher, store, clock)

    drain(job_worker)

    assert publisher.post_count == 4
    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.SCHEDULED
    refresh = session.scalars(select(JobRun).where(JobRun.job_type == REFRESH_PUBLICATIONS)).one()
    assert refresh.status is JobStatus.QUEUED  # waits for the posting time
    assert refresh.run_at > datetime(2026, 10, 1, 15, 0, tzinfo=UTC)

    for publication in session.scalars(select(Publication)):
        publisher.deliver(publication.external_id or "")
    clock.now = datetime(2026, 10, 1, 15, 10, tzinfo=UTC)
    drain(job_worker)

    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.PUBLISHED
    assert status_of(session, refresh) is JobStatus.SUCCEEDED
    assert all(
        p.status is PublicationRecordStatus.PUBLISHED and p.platform_post_id
        for p in session.scalars(select(Publication))
    )


def test_refresh_keeps_polling_until_every_platform_has_reported(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    experiment = approved(session, tmp_path, store)
    map_all(session, experiment.ip)
    publisher = FakePublisher(accounts=set(CHANNELS.values()))
    schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)
    job_worker = worker(session, publisher, store, clock)
    drain(job_worker)
    clock.now = datetime(2026, 10, 1, 15, 10, tzinfo=UTC)

    drain(job_worker)  # nothing delivered yet

    refresh = session.scalars(select(JobRun).where(JobRun.job_type == REFRESH_PUBLICATIONS)).one()
    assert refresh.status is JobStatus.QUEUED
    assert refresh.run_at == clock.now + timedelta(minutes=15)
    assert refresh.attempts == 0


def test_replaying_the_publish_job_does_not_duplicate_publications(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    experiment = approved(session, tmp_path, store)
    map_all(session, experiment.ip)
    publisher = FakePublisher(accounts=set(CHANNELS.values()))
    schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)
    job_worker = worker(session, publisher, store, clock)
    drain(job_worker)

    session.execute(
        update(JobRun)
        .where(JobRun.job_type == PUBLISH_VIDEO)
        .values(status=JobStatus.QUEUED, run_at=clock())
    )
    session.expire_all()
    drain(job_worker)

    assert publisher.post_count == 4
    assert len(session.scalars(select(Publication)).all()) == 4
    assert {p.platform for p in session.scalars(select(Publication))} == set(Platform)


def test_a_publisher_outage_is_retried_and_ends_with_one_post_per_platform(
    session: Session, tmp_path: Path, store: LocalAssetStore, clock: Clock
) -> None:
    class FlakyPublisher(FakePublisher):
        outages = 1

        def schedule(self, request):  # type: ignore[no-untyped-def]
            if request.platform is Platform.INSTAGRAM_REELS and self.outages:
                self.outages -= 1
                raise PublisherError("Buffer is down")
            return super().schedule(request)

    experiment = approved(session, tmp_path, store)
    map_all(session, experiment.ip)
    publisher = FlakyPublisher(accounts=set(CHANNELS.values()))
    schedule_ready_videos(session, now=clock(), schedule=SCHEDULE)
    job_worker = worker(session, publisher, store, clock)

    job_worker.run_once()
    job = session.scalars(select(JobRun).where(JobRun.job_type == PUBLISH_VIDEO)).one()
    assert job.status is JobStatus.QUEUED and "Buffer is down" in (job.error or "")
    clock.now = job.run_at
    drain(job_worker)

    assert status_of(session, job) is JobStatus.SUCCEEDED
    assert publisher.post_count == 4
