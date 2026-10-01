from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.scheduling.models import JobRun, JobStatus
from app.scheduling.queue import (
    claim_next,
    complete,
    enqueue,
    fail,
    requeue_stale,
    reschedule,
)
from tests.factories import make_experiment

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_enqueued_job_is_durable_and_linked_to_its_experiment(session: Session) -> None:
    experiment = make_experiment(session)

    job = enqueue(session, "produce_short", payload={"n": 1}, experiment_id=experiment.id, now=NOW)

    session.expire_all()
    stored = session.get_one(JobRun, job.id)
    assert stored.status is JobStatus.QUEUED
    assert stored.experiment_id == experiment.id
    assert stored.payload == {"n": 1}
    assert stored.attempts == 0
    assert stored.run_at == NOW


def test_enqueueing_the_same_idempotency_key_twice_creates_one_job(session: Session) -> None:
    first = enqueue(session, "produce_short", payload={}, idempotency_key="produce:1", now=NOW)
    second = enqueue(session, "produce_short", payload={}, idempotency_key="produce:1", now=NOW)

    assert second.id == first.id
    assert len(session.scalars(select(JobRun)).all()) == 1


def test_claiming_marks_the_oldest_due_job_running(session: Session) -> None:
    later = enqueue(session, "a", payload={}, now=NOW, run_at=NOW + timedelta(minutes=5))
    due = enqueue(session, "a", payload={}, now=NOW - timedelta(minutes=1))

    claimed = claim_next(session, worker_id="w1", now=NOW)

    assert claimed is not None and claimed.id == due.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.attempts == 1
    assert (claimed.locked_by, claimed.locked_at, claimed.started_at) == ("w1", NOW, NOW)
    assert claim_next(session, worker_id="w1", now=NOW) is None  # the other is not due yet
    assert session.get_one(JobRun, later.id).status is JobStatus.QUEUED


def test_claim_can_be_restricted_to_job_types(session: Session) -> None:
    enqueue(session, "publish", payload={}, now=NOW)
    wanted = enqueue(session, "analytics", payload={}, now=NOW)

    claimed = claim_next(session, worker_id="w1", now=NOW, job_types=["analytics"])

    assert claimed is not None and claimed.id == wanted.id


def test_two_workers_never_claim_the_same_job(committed_db: Engine) -> None:
    with Session(committed_db) as setup:
        enqueue(setup, "a", payload={}, now=NOW)
        setup.commit()

    with Session(committed_db) as first, Session(committed_db) as second:
        # Hold the first worker's row lock open while the second one looks.
        job = first.scalars(
            select(JobRun)
            .where(JobRun.status == JobStatus.QUEUED)
            .with_for_update(skip_locked=True)
        ).one()
        assert claim_next(second, worker_id="w2", now=NOW) is None
        first.rollback()
        claimed = claim_next(second, worker_id="w2", now=NOW)
        assert claimed is not None and claimed.id == job.id


def test_completed_job_records_result_and_finish_time(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW)
    job = claim_next(session, worker_id="w1", now=NOW)
    assert job is not None

    complete(session, job, result={"asset_id": "x"}, now=NOW + timedelta(seconds=30))

    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"asset_id": "x"}
    assert job.finished_at == NOW + timedelta(seconds=30)
    assert job.locked_by is None


def test_failed_job_is_retried_later_with_backoff(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW, max_attempts=3)
    job = claim_next(session, worker_id="w1", now=NOW)
    assert job is not None

    fail(session, job, error="provider timeout", now=NOW)

    assert job.status is JobStatus.QUEUED
    assert job.error == "provider timeout"
    assert job.run_at > NOW
    assert job.finished_at is None
    assert claim_next(session, worker_id="w1", now=NOW) is None
    retried = claim_next(session, worker_id="w1", now=job.run_at)
    assert retried is not None and retried.attempts == 2


def test_backoff_grows_with_each_attempt(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW, max_attempts=5)
    delays = []
    for _ in range(3):
        job = claim_next(session, worker_id="w1", now=NOW + timedelta(days=1))
        assert job is not None
        fail(session, job, error="x", now=NOW)
        delays.append(job.run_at - NOW)

    assert delays[0] < delays[1] < delays[2]


def test_job_fails_for_good_after_its_last_attempt(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW, max_attempts=1)
    job = claim_next(session, worker_id="w1", now=NOW)
    assert job is not None

    fail(session, job, error="still broken", now=NOW)

    assert job.status is JobStatus.FAILED
    assert job.finished_at == NOW
    assert job.error == "still broken"


def test_permanent_failure_is_not_retried(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW, max_attempts=5)
    job = claim_next(session, worker_id="w1", now=NOW)
    assert job is not None

    fail(session, job, error="illegal state", now=NOW, retryable=False)

    assert job.status is JobStatus.FAILED


def test_rescheduling_does_not_consume_an_attempt(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW, max_attempts=1)
    job = claim_next(session, worker_id="w1", now=NOW)
    assert job is not None

    reschedule(session, job, delay=timedelta(seconds=10), now=NOW)

    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.run_at == NOW + timedelta(seconds=10)


def test_job_abandoned_by_a_crashed_worker_is_requeued(session: Session) -> None:
    enqueue(session, "a", payload={}, now=NOW)
    crashed = claim_next(session, worker_id="dead-worker", now=NOW)
    assert crashed is not None
    lock_timeout = timedelta(minutes=30)

    assert requeue_stale(session, lock_timeout=lock_timeout, now=NOW + timedelta(minutes=5)) == 0
    assert requeue_stale(session, lock_timeout=lock_timeout, now=NOW + timedelta(minutes=31)) == 1

    assert crashed.status is JobStatus.QUEUED
    assert crashed.locked_by is None
    resumed = claim_next(session, worker_id="w2", now=NOW + timedelta(minutes=31))
    assert resumed is not None and resumed.id == crashed.id and resumed.attempts == 2
