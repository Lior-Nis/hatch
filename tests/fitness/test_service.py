from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.ingestion import ingest_publication
from app.analytics.jobs import analytics_handlers, schedule_observations
from app.analytics.models import MetricSnapshot
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.fitness.heuristic import HeuristicFitnessEvaluator
from app.fitness.jobs import EVALUATE_FITNESS, fitness_handlers
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.fitness.service import account_baseline, evaluate_snapshot
from app.platforms import Platform
from app.scheduling.models import JobRun
from app.scheduling.worker import Worker
from integrations.fake.analytics import FakeAnalyticsAdapter
from tests.analytics.test_ingestion import PUBLISHED_AT, Clock, publication_on, published_experiment

GOOD: dict[str, Any] = {
    "views": 1000, "average_watch_fraction": 0.8, "completion_rate": 0.45,
    "likes": 40, "shares": 6, "saves": 6, "follows": 3,
}  # fmt: skip
TYPICAL: dict[str, Any] = {
    "views": 800, "average_watch_fraction": 0.6, "completion_rate": 0.3,
    "likes": 24, "shares": 2, "saves": 4, "follows": 2,
}  # fmt: skip
TARGET = Decimal("0.50")


def observe(
    session: Session,
    experiment: Experiment,
    payload: dict[str, Any],
    *,
    platform: Platform = Platform.TIKTOK,
    checkpoint: str = "72h",
) -> MetricSnapshot:
    publication = publication_on(session, experiment, platform)
    adapter = FakeAnalyticsAdapter(
        platform=platform, payloads={publication.platform_post_id or "": payload}
    )
    return ingest_publication(
        session, publication.id, adapter, checkpoint=checkpoint,
        now=PUBLISHED_AT + timedelta(hours=72),
    )  # fmt: skip


def evaluate(session: Session, snapshot: MetricSnapshot) -> FitnessSnapshot | None:
    return evaluate_snapshot(
        session, snapshot.id, HeuristicFitnessEvaluator(), target_cost_usd=TARGET
    )


def test_fitness_snapshot_links_metric_publication_experiment_and_formula(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    snapshot = observe(session, experiment, GOOD)

    fitness = evaluate(session, snapshot)

    assert fitness is not None
    assert fitness.scope is FitnessScope.VIDEO
    assert fitness.metric_snapshot_id == snapshot.id
    assert fitness.publication_id == snapshot.publication_id
    assert fitness.experiment_id == experiment.id
    assert fitness.ip_id == experiment.ip_id
    assert fitness.platform is Platform.TIKTOK
    assert fitness.checkpoint == "72h"
    assert (fitness.evaluator, fitness.evaluator_version) == ("heuristic", "1")
    assert fitness.score > 0.5
    assert fitness.components["retention"] > 0
    assert fitness.inputs["metrics"]["views"] == 1000
    assert fitness.inputs["confidence"] > 0
    assert "references" in fitness.inputs["details"]


def test_evaluating_the_same_snapshot_twice_gives_one_fitness_record(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    snapshot = observe(session, experiment, GOOD)

    first = evaluate(session, snapshot)
    second = evaluate(session, snapshot)

    assert first is not None and second is not None and first.id == second.id
    assert len(session.scalars(select(FitnessSnapshot)).all()) == 1


def test_production_cost_comes_from_the_ledger(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)

    fitness = evaluate(session, observe(session, experiment, GOOD))

    assert fitness is not None
    assert Decimal(fitness.inputs["production_cost_usd"]) == Decimal("0.40")
    assert Decimal(fitness.inputs["target_cost_usd"]) == TARGET


def test_views_only_metrics_yield_no_fitness_record(session: Session, tmp_path: Path) -> None:
    experiment = published_experiment(session, tmp_path)

    fitness = evaluate(session, observe(session, experiment, {"views": 250_000}))

    assert fitness is None
    assert session.scalars(select(FitnessSnapshot)).all() == []


def test_account_baseline_needs_history_and_excludes_the_video_itself(
    session: Session, tmp_path: Path
) -> None:
    earlier = [published_experiment(session, tmp_path) for _ in range(3)]
    for experiment in earlier:
        observe(session, experiment, TYPICAL)
    current = published_experiment(session, tmp_path)
    snapshot = observe(session, current, GOOD)
    publication = publication_on(session, current, Platform.TIKTOK)

    baseline = account_baseline(session, publication, "72h")

    assert baseline is not None
    assert baseline.views == 800  # the median of the three earlier videos, not 1000
    assert baseline.average_watch_fraction == pytest.approx(0.6)
    fitness = evaluate(session, snapshot)
    assert fitness is not None
    assert fitness.inputs["details"]["references"]["retention"]["source"] == "account_baseline"
    assert fitness.components["distribution"] > 0


def test_with_too_little_history_platform_priors_are_used(session: Session, tmp_path: Path) -> None:
    first = published_experiment(session, tmp_path)
    observe(session, first, TYPICAL)
    current = published_experiment(session, tmp_path)
    publication = publication_on(session, current, Platform.TIKTOK)

    assert account_baseline(session, publication, "72h") is None


def test_baselines_are_never_pooled_across_platforms(session: Session, tmp_path: Path) -> None:
    earlier = [published_experiment(session, tmp_path) for _ in range(3)]
    for experiment in earlier:
        observe(session, experiment, TYPICAL, platform=Platform.YOUTUBE_SHORTS)
    current = published_experiment(session, tmp_path)

    tiktok = account_baseline(session, publication_on(session, current, Platform.TIKTOK), "72h")
    youtube = account_baseline(
        session, publication_on(session, current, Platform.YOUTUBE_SHORTS), "72h"
    )

    assert tiktok is None
    assert youtube is not None


# --- the 72h checkpoint, through the queue ------------------------------------------


def video_scores(session: Session) -> list[FitnessSnapshot]:
    return list(
        session.scalars(select(FitnessSnapshot).where(FitnessSnapshot.scope == FitnessScope.VIDEO))
    )


def run_jobs(session: Session, adapters: dict[Platform, Any], clock: Clock) -> None:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = {
        **analytics_handlers(adapters),
        **fitness_handlers(HeuristicFitnessEvaluator(), target_cost_usd=TARGET),
    }
    worker = Worker(sessions, handlers, worker_id="w1", clock=clock)
    for _ in range(100):
        if not worker.run_once():
            return


def test_72h_is_the_first_evaluation_and_later_windows_keep_updating(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    schedule_observations(session, publication)
    adapters = {
        Platform.TIKTOK: FakeAnalyticsAdapter(
            platform=Platform.TIKTOK, payloads={publication.platform_post_id or "": GOOD}
        )
    }
    clock = Clock()

    clock.now = PUBLISHED_AT + timedelta(hours=25)
    run_jobs(session, adapters, clock)
    session.expire_all()
    assert session.scalars(select(FitnessSnapshot)).all() == []  # 1h/6h/24h: counters only
    assert session.get_one(Experiment, experiment.id).status is ExperimentStatus.OBSERVING

    clock.now = PUBLISHED_AT + timedelta(hours=73)
    run_jobs(session, adapters, clock)
    session.expire_all()
    [early] = video_scores(session)
    assert early.checkpoint == "72h"
    ip_score = session.scalars(
        select(FitnessSnapshot).where(FitnessSnapshot.scope == FitnessScope.IP)
    ).one()
    assert ip_score.inputs["sufficient_evidence"] is False  # one video is not enough
    assert session.get_one(Experiment, experiment.id).status is ExperimentStatus.EARLY_EVALUATED

    clock.now = PUBLISHED_AT + timedelta(days=31)
    run_jobs(session, adapters, clock)
    session.expire_all()
    checkpoints = {f.checkpoint for f in video_scores(session)}
    assert checkpoints == {"72h", "7d", "30d"}
    matured = session.get_one(Experiment, experiment.id)
    assert matured.status is ExperimentStatus.MATURED
    assert matured.video_status is VideoStatus.EVALUATED
    jobs = session.scalars(select(JobRun).where(JobRun.job_type == EVALUATE_FITNESS)).all()
    assert len(jobs) == 3 and {job.experiment_id for job in jobs} == {experiment.id}


def test_the_checkpoint_passes_even_when_evidence_is_too_thin_to_score(
    session: Session, tmp_path: Path
) -> None:
    experiment = published_experiment(session, tmp_path)
    publication = publication_on(session, experiment, Platform.TIKTOK)
    schedule_observations(session, publication)
    adapters = {
        Platform.TIKTOK: FakeAnalyticsAdapter(
            platform=Platform.TIKTOK, payloads={publication.platform_post_id or "": {"views": 9}}
        )
    }
    clock = Clock()
    clock.now = PUBLISHED_AT + timedelta(hours=73)

    run_jobs(session, adapters, clock)

    session.expire_all()
    assert session.scalars(select(FitnessSnapshot)).all() == []
    assert session.get_one(Experiment, experiment.id).status is ExperimentStatus.EARLY_EVALUATED
    job = session.scalars(select(JobRun).where(JobRun.job_type == EVALUATE_FITNESS)).one()
    assert job.result is not None and job.result["scored"] is False
    assert "views" in job.result["reason"]
