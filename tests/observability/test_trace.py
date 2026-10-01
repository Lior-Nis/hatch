from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.observability.trace import experiment_timeline, render_timeline
from app.production.jobs import PRODUCE_SHORT, enqueue_production, produce_short_handler
from app.production.run import ProductionDeps
from app.quality.jobs import RUN_QA, run_qa_handler
from app.quality.models import ReviewDecision
from app.quality.review import submit_review
from app.quality.technical import TechnicalQAGate
from app.scheduling.worker import Worker
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore
from tests.factories import LIMITS, make_experiment


def run_pipeline(session: Session, tmp_path: Path, generator: FakeMediaGenerator) -> None:
    store = LocalAssetStore(tmp_path / "assets")
    deps = ProductionDeps(generator=generator, store=store, governor=BudgetGovernor(LIMITS))

    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    worker = Worker(
        sessions,
        {
            PRODUCE_SHORT: produce_short_handler(deps, poll_interval=timedelta(0)),
            RUN_QA: run_qa_handler(gates=[TechnicalQAGate()], store=store),
        },
        worker_id="w1",
    )
    while worker.run_once():
        pass


def test_timeline_traces_a_failed_experiment_end_to_end(session: Session, tmp_path: Path) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id)
    run_pipeline(session, tmp_path, FakeMediaGenerator(fail_next=["nsfw: flagged by provider"] * 3))

    events = experiment_timeline(session, experiment.id)

    kinds = [event.kind for event in events]
    assert kinds[0] == "experiment_created"
    assert "job_queued" in kinds
    assert "budget_reserved" in kinds
    assert "generation_started" in kinds
    failure = next(event for event in events if event.kind == "generation_failed")
    assert failure.level == "error"
    assert "nsfw: flagged by provider" in failure.detail
    assert kinds.index("budget_reserved") < kinds.index("generation_failed")
    assert events == sorted(events, key=lambda event: event.at)
    assert events[-1].kind == "current_state"
    assert "generation_failed" in events[-1].detail


def test_timeline_of_a_successful_reviewed_video_shows_every_stage(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id)
    run_pipeline(session, tmp_path, FakeMediaGenerator())
    submit_review(
        session, experiment.id, decision=ReviewDecision.REJECT, reason="Off-model.", reviewer="lior"
    )

    events = experiment_timeline(session, experiment.id)

    kinds = [event.kind for event in events]
    for expected in (
        "generation_succeeded",
        "asset_stored",
        "qa_pass",
        "human_reject",
        "job_succeeded",
    ):
        assert expected in kinds, expected
    review = next(event for event in events if event.kind == "human_reject")
    assert "Off-model." in review.detail and "lior" in review.detail


def test_timeline_shows_budget_blocks(session: Session, tmp_path: Path) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id)
    run_pipeline(session, tmp_path, FakeMediaGenerator(cost_per_second_usd=Decimal("0.50")))

    events = experiment_timeline(session, experiment.id)

    blocked = next(event for event in events if event.kind == "budget_blocked")
    assert blocked.level == "error"
    assert "per_generation" in blocked.detail


def test_rendered_timeline_is_readable_text(session: Session, tmp_path: Path) -> None:
    experiment = make_experiment(session)
    enqueue_production(session, experiment.id)
    run_pipeline(session, tmp_path, FakeMediaGenerator(fail_next=["provider outage"] * 3))

    text = render_timeline(experiment_timeline(session, experiment.id))

    assert "generation_failed" in text
    assert "provider outage" in text
    assert text.splitlines()[0].startswith(str(datetime.now(UTC).year))
