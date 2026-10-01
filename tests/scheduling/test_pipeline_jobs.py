"""The production pipeline run through the durable queue."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.budgets.models import BudgetLedgerEntry
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.jobs import PRODUCE_SHORT, enqueue_production, produce_short_handler
from app.production.models import Asset, GenerationAttempt
from app.production.run import ProductionDeps
from app.quality.jobs import RUN_QA, run_qa_handler
from app.quality.models import QAResult
from app.quality.technical import TechnicalQAGate
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.queue import claim_next
from app.scheduling.worker import Worker
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore
from tests.factories import LIMITS, make_experiment


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_worker(
    session: Session, tmp_path: Path, generator: FakeMediaGenerator, clock: Clock
) -> Worker:
    store = LocalAssetStore(tmp_path / "assets")
    deps = ProductionDeps(generator=generator, store=store, governor=BudgetGovernor(LIMITS))

    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = {
        PRODUCE_SHORT: produce_short_handler(deps, poll_interval=timedelta(seconds=10)),
        RUN_QA: run_qa_handler(gates=[TechnicalQAGate()], store=store),
    }
    return Worker(sessions, handlers, worker_id="w1", clock=clock)


def drain(worker: Worker, clock: Clock, *, limit: int = 20) -> None:
    for _ in range(limit):
        if not worker.run_once():
            clock.advance(seconds=15)
            if not worker.run_once():
                return
    raise AssertionError("queue did not drain")


def jobs(session: Session) -> dict[str, JobRun]:
    return {job.job_type: job for job in session.scalars(select(JobRun)).all()}


def test_enqueued_experiment_is_produced_and_checked_by_the_worker(
    session: Session, tmp_path: Path, clock: Clock
) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id, now=clock())

    drain(make_worker(session, tmp_path, FakeMediaGenerator(), clock), clock)

    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
    by_type = jobs(session)
    assert by_type[PRODUCE_SHORT].status is JobStatus.SUCCEEDED
    assert by_type[RUN_QA].status is JobStatus.SUCCEEDED
    assert {job.experiment_id for job in by_type.values()} == {experiment.id}
    result = by_type[PRODUCE_SHORT].result
    assert result is not None and result["video_status"] == "generated"


def test_enqueueing_production_twice_creates_one_job(session: Session, clock: Clock) -> None:
    experiment = make_experiment(session)

    first = enqueue_production(session, experiment.id, now=clock())
    second = enqueue_production(session, experiment.id, now=clock())

    assert first.id == second.id


def test_slow_generation_is_polled_without_blocking_or_resubmitting(
    session: Session, tmp_path: Path, clock: Clock
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(polls_until_done=2)
    enqueue_production(session, experiment.id, now=clock())
    worker = make_worker(session, tmp_path, generator, clock)

    worker.run_once()

    job = jobs(session)[PRODUCE_SHORT]
    assert job.status is JobStatus.QUEUED
    assert job.run_at == clock() + timedelta(seconds=10)
    assert job.attempts == 0
    drain(worker, clock)
    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
    assert len(generator.submitted_requests) == 1


def test_replaying_a_successful_job_does_not_duplicate_media_or_charges(
    session: Session, tmp_path: Path, clock: Clock
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))
    enqueue_production(session, experiment.id, now=clock())
    worker = make_worker(session, tmp_path, generator, clock)
    drain(worker, clock)

    session.execute(update(JobRun).values(status=JobStatus.QUEUED, run_at=clock()))
    session.expire_all()
    drain(worker, clock)

    assert len(generator.submitted_requests) == 1
    assert len(session.scalars(select(GenerationAttempt)).all()) == 1
    assert len(session.scalars(select(Asset)).all()) == 1
    assert len(session.scalars(select(BudgetLedgerEntry)).all()) == 1
    assert len(session.scalars(select(QAResult)).all()) == 1
    assert len(session.scalars(select(JobRun)).all()) == 2
    assert all(job.status is JobStatus.SUCCEEDED for job in jobs(session).values())


def test_generation_survives_a_worker_crash_without_paying_twice(
    session: Session, tmp_path: Path, clock: Clock
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(polls_until_done=1)
    enqueue_production(session, experiment.id, now=clock())
    crashed_worker = make_worker(session, tmp_path, generator, clock)
    crashed_worker.run_once()  # submits the provider job, then waits
    clock.advance(seconds=15)
    abandoned = claim_next(session, worker_id="dead-worker", now=clock())
    assert abandoned is not None  # claimed, then the process dies

    clock.advance(minutes=31)
    drain(make_worker(session, tmp_path, generator, clock), clock)

    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
    assert len(generator.submitted_requests) == 1
    assert len(session.scalars(select(BudgetLedgerEntry)).all()) == 1


def test_failed_generation_finishes_the_job_and_skips_qa(
    session: Session, tmp_path: Path, clock: Clock
) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id, now=clock())

    drain(make_worker(session, tmp_path, FakeMediaGenerator(fail_next=["nsfw"]), clock), clock)

    by_type = jobs(session)
    assert set(by_type) == {PRODUCE_SHORT}
    assert by_type[PRODUCE_SHORT].status is JobStatus.SUCCEEDED
    assert by_type[PRODUCE_SHORT].result == {
        "video_status": "generation_failed",
        "asset_id": None,
        "error": "nsfw",
    }
