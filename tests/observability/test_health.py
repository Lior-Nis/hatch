from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.experiments.models import Experiment
from app.observability.health import find_stalls, provider_health
from app.production.run import ProductionDeps, produce_short
from app.scheduling.models import JobRun
from app.scheduling.queue import claim_next, enqueue
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore
from tests.factories import LIMITS, make_experiment

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def produce(
    session: Session, tmp_path: Path, generator: FakeMediaGenerator, **deps: float
) -> Experiment:
    experiment = make_experiment(session)
    produce_short(
        session,
        experiment.id,
        ProductionDeps(
            generator=generator,
            store=LocalAssetStore(tmp_path / "assets"),
            governor=BudgetGovernor(LIMITS),
            poll_interval_seconds=0.0,
            sleep=lambda seconds: None,
            **deps,
        ),
    )
    return experiment


def test_provider_health_reports_failure_rate_and_wasted_spend(
    session: Session, tmp_path: Path
) -> None:
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"), fail_next=["outage"])
    produce(session, tmp_path, generator)  # fails
    produce(session, tmp_path, generator)  # succeeds
    produce(session, tmp_path, generator)  # succeeds

    [health] = provider_health(session)

    assert (health.provider, health.model) == ("fake", "alibaba/wan-3.0/text-to-video")
    assert (health.attempts, health.succeeded, health.failed) == (3, 2, 1)
    assert health.failure_rate == 1 / 3
    assert health.last_error == "outage"


def test_blocked_attempts_are_not_provider_failures(session: Session, tmp_path: Path) -> None:
    produce(session, tmp_path, FakeMediaGenerator(cost_per_second_usd=Decimal("0.50")))

    [health] = provider_health(session)

    assert (health.attempts, health.failed, health.blocked_by_budget) == (0, 0, 1)
    assert health.failure_rate is None


def test_a_healthy_pipeline_has_no_stalls(session: Session, tmp_path: Path) -> None:
    produce(session, tmp_path, FakeMediaGenerator())

    assert find_stalls(session) == []


def test_video_stuck_generating_is_reported_after_the_threshold(
    session: Session, tmp_path: Path
) -> None:
    experiment = produce(
        session, tmp_path, FakeMediaGenerator(polls_until_done=99), timeout_seconds=0.0
    )
    session.execute(update(Experiment).values(video_status_changed_at=NOW))
    threshold = timedelta(minutes=30)

    assert find_stalls(session, now=NOW + timedelta(minutes=29), stall_after=threshold) == []
    [stall] = find_stalls(session, now=NOW + timedelta(minutes=31), stall_after=threshold)

    assert stall.kind == "video_stalled"
    assert stall.experiment_id == experiment.id
    assert "generating" in stall.detail


def test_videos_waiting_for_a_human_are_not_stalls(session: Session) -> None:
    experiment = make_experiment(session)
    session.execute(
        update(Experiment)
        .where(Experiment.id == experiment.id)
        .values(video_status="approval_pending", video_status_changed_at=NOW)
    )

    assert find_stalls(session, now=NOW + timedelta(days=3)) == []


def test_job_held_by_a_dead_worker_is_reported(session: Session) -> None:
    enqueue(session, "produce_short", payload={}, now=NOW)
    job = claim_next(session, worker_id="dead", now=NOW)
    assert job is not None

    [stall] = find_stalls(session, now=NOW + timedelta(hours=2))

    assert stall.kind == "job_stuck"
    assert stall.job_id == job.id


def test_overdue_queued_job_means_no_worker_is_running(session: Session) -> None:
    job = enqueue(session, "run_qa", payload={}, now=NOW)

    assert find_stalls(session, now=NOW + timedelta(minutes=5)) == []
    [stall] = find_stalls(session, now=NOW + timedelta(hours=1))

    assert stall.kind == "queue_not_draining"
    assert stall.job_id == job.id
    assert session.get_one(JobRun, job.id).attempts == 0
