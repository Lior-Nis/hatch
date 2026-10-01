from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.production.ports import ProviderError
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobResult, PermanentJobError, RetryLater, Worker

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def worker(session: Session, handlers: dict[str, object]) -> Worker:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    return Worker(
        sessions,
        handlers,  # type: ignore[arg-type]
        worker_id="test-worker",
        clock=lambda: NOW,
    )


def test_worker_runs_the_handler_and_marks_the_job_succeeded(session: Session) -> None:
    seen: list[dict[str, object]] = []

    def handler(session: Session, job: JobRun) -> JobResult:
        seen.append(job.payload)
        return {"ok": True}

    job = enqueue(session, "greet", payload={"name": "Nib"}, now=NOW)

    assert worker(session, {"greet": handler}).run_once() is True

    assert seen == [{"name": "Nib"}]
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"ok": True}


def test_worker_reports_idle_when_nothing_is_due(session: Session) -> None:
    assert worker(session, {}).run_once() is False


def test_handler_error_is_recorded_and_the_job_is_retried(session: Session) -> None:
    def handler(session: Session, job: JobRun) -> JobResult:
        raise ProviderError("502 from provider")

    job = enqueue(session, "flaky", payload={}, now=NOW)

    worker(session, {"flaky": handler}).run_once()

    assert job.status is JobStatus.QUEUED
    assert job.error is not None and "502 from provider" in job.error
    assert job.run_at > NOW


def test_permanent_error_fails_the_job_immediately(session: Session) -> None:
    def handler(session: Session, job: JobRun) -> JobResult:
        raise PermanentJobError("experiment was rejected")

    job = enqueue(session, "doomed", payload={}, now=NOW, max_attempts=5)

    worker(session, {"doomed": handler}).run_once()

    assert job.status is JobStatus.FAILED
    assert job.error == "experiment was rejected"


def test_retry_later_requeues_without_counting_a_failure(session: Session) -> None:
    def handler(session: Session, job: JobRun) -> JobResult:
        raise RetryLater(timedelta(seconds=20), "still generating")

    job = enqueue(session, "poll", payload={}, now=NOW, max_attempts=1)

    worker(session, {"poll": handler}).run_once()

    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.run_at == NOW + timedelta(seconds=20)


def test_job_with_no_registered_handler_fails_visibly(session: Session) -> None:
    job = enqueue(session, "unknown_type", payload={}, now=NOW)

    worker(session, {}).run_once()

    assert job.status is JobStatus.FAILED
    assert job.error is not None and "no handler" in job.error


def test_work_done_by_a_failing_handler_is_rolled_back(session: Session) -> None:
    def handler(session: Session, job: JobRun) -> JobResult:
        enqueue(session, "side_effect", payload={}, now=NOW)
        raise ProviderError("boom")

    enqueue(session, "half_done", payload={}, now=NOW)

    worker(session, {"half_done": handler}).run_once()

    job_types = {job.job_type for job in session.query(JobRun).all()}
    assert job_types == {"half_done"}


def test_run_forever_survives_an_error_outside_job_handling(session: Session) -> None:
    calls = {"count": 0}

    class Flaky(Worker):
        def run_once(self) -> bool:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("database went away")
            return False

    sleeps: list[float] = []
    flaky = Flaky(lambda: None, {}, worker_id="w1")  # type: ignore[arg-type,return-value]

    flaky.run_forever(
        poll_interval_seconds=1.0, sleep=sleeps.append, should_stop=lambda: calls["count"] >= 3
    )

    assert calls["count"] == 3  # kept going after the error
    assert sleeps[0] > 1.0  # backed off after the failure before polling normally
