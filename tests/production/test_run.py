from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor, BudgetLimits
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.production.models import Asset, AssetKind, AttemptStatus
from app.production.probe import probe_media
from app.production.run import ProductionDeps, RetryPolicy, produce_short
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore
from tests.factories import final_asset, make_experiment

NO_RETRY = RetryPolicy(max_attempts_per_scene=1)

LIMITS = BudgetLimits(
    max_per_generation_usd=Decimal("0.75"),
    max_per_video_usd=Decimal("1.50"),
    daily_usd=Decimal("15.00"),
    monthly_usd=Decimal("500.00"),
)


def deps(tmp_path: Path, generator: FakeMediaGenerator, **overrides: object) -> ProductionDeps:
    fields: dict[str, object] = {
        "generator": generator,
        "store": LocalAssetStore(tmp_path / "assets"),
        "governor": BudgetGovernor(LIMITS),
        "poll_interval_seconds": 0.0,
        "timeout_seconds": 5.0,
        "sleep": lambda seconds: None,
    }
    fields.update(overrides)
    return ProductionDeps(**fields)  # type: ignore[arg-type]


def test_fixture_experiment_produces_a_playable_vertical_short(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))
    production = deps(tmp_path, generator)

    result = produce_short(session, experiment.id, production)

    session.expire_all()
    loaded = session.get_one(Experiment, experiment.id)
    assert loaded.video_status is VideoStatus.GENERATED
    assert loaded.status is ExperimentStatus.RUNNING
    asset = final_asset(loaded)
    assert result.asset_id == asset.id
    assert asset.kind is AssetKind.FINAL_VIDEO
    assert asset.mime_type == "video/mp4"
    assert (asset.media_info["width"], asset.media_info["height"]) == (1080, 1920)
    stored_file = production.store.local_path(asset.storage_uri)
    assert stored_file.stat().st_size == asset.size_bytes
    assert probe_media(stored_file).height == 1920


def test_successful_attempt_records_model_prompt_config_timestamps_and_cost(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))

    produce_short(session, experiment.id, deps(tmp_path, generator))

    session.expire_all()
    loaded = session.get_one(Experiment, experiment.id)
    [attempt] = loaded.generation_attempts
    assert attempt.status is AttemptStatus.SUCCEEDED
    assert attempt.provider == "fake"
    assert attempt.model == loaded.genome.genes["video_model"]
    assert attempt.prompt == generator.submitted_requests[0].prompt
    assert "Nib" in attempt.prompt
    assert attempt.request["duration_seconds"] == 8
    assert attempt.request["aspect_ratio"] == "9:16"
    assert attempt.provider_job_id is not None
    assert attempt.started_at is not None and attempt.finished_at is not None
    assert attempt.started_at <= attempt.finished_at
    assert attempt.estimated_cost_usd == Decimal("0.40")
    assert attempt.actual_cost_usd == Decimal("0.40")
    asset = final_asset(loaded)
    assert asset.generation_attempt_id == attempt.id
    [entry] = loaded.ledger_entries
    assert entry.status is LedgerStatus.SETTLED
    assert entry.generation_attempt_id == attempt.id
    assert entry.actual_cost_usd == Decimal("0.40")


def test_provider_failure_is_recorded_and_no_asset_is_created(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(fail_next=["content policy rejection"])

    result = produce_short(session, experiment.id, deps(tmp_path, generator, retry=NO_RETRY))

    session.expire_all()
    loaded = session.get_one(Experiment, experiment.id)
    assert result.asset_id is None
    assert loaded.video_status is VideoStatus.GENERATION_FAILED
    [attempt] = loaded.generation_attempts
    assert attempt.status is AttemptStatus.FAILED
    assert attempt.error == "content policy rejection"
    assert attempt.finished_at is not None
    assert loaded.assets == []
    [entry] = loaded.ledger_entries
    assert entry.status is LedgerStatus.SETTLED


def test_generation_over_budget_is_blocked_before_any_paid_call(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.20"))  # 8s → $1.60

    result = produce_short(session, experiment.id, deps(tmp_path, generator))

    session.expire_all()
    loaded = session.get_one(Experiment, experiment.id)
    assert generator.submitted_requests == []
    assert result.asset_id is None
    assert loaded.video_status is VideoStatus.ABORTED_BUDGET
    [attempt] = loaded.generation_attempts
    assert attempt.status is AttemptStatus.BLOCKED_BUDGET
    assert attempt.error is not None and "per_generation" in attempt.error
    [entry] = loaded.ledger_entries
    assert entry.status is LedgerStatus.BLOCKED


def test_rerunning_a_finished_production_does_not_generate_or_charge_again(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator()
    production = deps(tmp_path, generator)
    first = produce_short(session, experiment.id, production)

    second = produce_short(session, experiment.id, production)

    assert second.asset_id == first.asset_id
    assert len(generator.submitted_requests) == 1
    finals = select(Asset).where(Asset.kind == AssetKind.FINAL_VIDEO)
    assert len(session.scalars(finals).all()) == 1
    assert len(session.scalars(select(BudgetLedgerEntry)).all()) == 1


def test_interrupted_production_resumes_the_same_provider_job(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(polls_until_done=3)
    impatient = deps(tmp_path, generator, timeout_seconds=0.0)

    waiting = produce_short(session, experiment.id, impatient)
    assert waiting.asset_id is None
    assert waiting.video_status is VideoStatus.GENERATING

    finished = produce_short(session, experiment.id, deps(tmp_path, generator))

    session.expire_all()
    loaded = session.get_one(Experiment, experiment.id)
    assert finished.asset_id is not None
    assert loaded.video_status is VideoStatus.GENERATED
    assert len(generator.submitted_requests) == 1
    assert len(loaded.generation_attempts) == 1
    assert len(loaded.ledger_entries) == 1


def test_production_refuses_an_experiment_whose_video_already_failed(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    failing = FakeMediaGenerator(fail_next=["boom"])
    produce_short(session, experiment.id, deps(tmp_path, failing, retry=NO_RETRY))
    healthy = FakeMediaGenerator()

    with pytest.raises(ValueError, match="generation_failed"):
        produce_short(session, experiment.id, deps(tmp_path, healthy))

    assert healthy.submitted_requests == []
