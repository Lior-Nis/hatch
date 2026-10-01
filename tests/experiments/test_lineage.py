import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor, BudgetLimits
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.lineage import ExperimentNotFound, get_lineage
from app.production.run import ProductionDeps, RetryPolicy, produce_short
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_experiment

LIMITS = BudgetLimits(
    max_per_generation_usd=Decimal("0.75"),
    max_per_video_usd=Decimal("1.50"),
    daily_usd=Decimal("15.00"),
    monthly_usd=Decimal("500.00"),
)


def produce(session: Session, tmp_path: Path, generator: FakeMediaGenerator) -> uuid.UUID:
    experiment = make_experiment(session)
    deps = ProductionDeps(
        generator=generator,
        store=LocalAssetStore(tmp_path / "assets"),
        governor=BudgetGovernor(LIMITS),
        poll_interval_seconds=0.0,
        sleep=lambda seconds: None,
        retry=RetryPolicy(max_attempts_per_scene=1),
    )
    produce_short(session, experiment.id, deps)
    session.expire_all()
    return experiment.id


def test_lineage_explains_why_the_video_exists(session: Session, tmp_path: Path) -> None:
    experiment_id = produce(session, tmp_path, FakeMediaGenerator())

    lineage = get_lineage(session, experiment_id)

    assert lineage.experiment.generation_reason == FIRST_SHORT.generation_reason
    assert lineage.experiment.lineage_id is not None
    assert lineage.experiment.parent_experiment_ids == []
    assert lineage.ip.slug == "nibbin-hollow"
    assert lineage.hypothesis.statement == FIRST_SHORT.hypothesis.statement
    assert lineage.hypothesis.prediction["metric"] == "completion_rate"
    assert lineage.genome.genes["hook_type"] == "visual_question"
    assert lineage.genome.creative_spec == FIRST_SHORT.genome.creative_spec


def test_lineage_explains_how_the_video_was_generated(session: Session, tmp_path: Path) -> None:
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))
    experiment_id = produce(session, tmp_path, generator)

    lineage = get_lineage(session, experiment_id)

    [attempt] = lineage.generation_attempts
    assert attempt.provider == "fake"
    assert attempt.model == "alibaba/wan-3.0/text-to-video"
    assert attempt.prompt == generator.submitted_requests[0].prompt
    assert attempt.request["resolution"] == "480p"
    assert attempt.status == "succeeded"
    assert attempt.estimated_cost_usd == Decimal("0.40")
    assert attempt.started_at is not None and attempt.finished_at is not None
    assert lineage.final_asset is not None
    assert lineage.final_asset.generation_attempt_id == attempt.id
    assert len(lineage.final_asset.sha256) == 64
    assert lineage.final_asset.media_info["height"] == 1920
    [entry] = lineage.ledger
    assert entry.status == "settled"
    assert lineage.cost.committed_usd == Decimal("0.40")
    assert lineage.cost.attempts == 1


def test_lineage_keeps_failed_attempts_and_has_no_final_asset(
    session: Session, tmp_path: Path
) -> None:
    experiment_id = produce(session, tmp_path, FakeMediaGenerator(fail_next=["provider outage"]))

    lineage = get_lineage(session, experiment_id)

    assert lineage.experiment.video_status == "generation_failed"
    assert lineage.final_asset is None
    assert [a.error for a in lineage.generation_attempts] == ["provider outage"]
    assert lineage.cost.committed_usd == Decimal("0")


def test_lineage_is_json_serialisable(session: Session, tmp_path: Path) -> None:
    experiment_id = produce(session, tmp_path, FakeMediaGenerator())

    document = json.loads(get_lineage(session, experiment_id).model_dump_json())

    assert document["experiment"]["id"] == str(experiment_id)
    assert document["final_asset"]["kind"] == "final_video"


def test_lineage_of_an_unknown_experiment_raises(session: Session) -> None:
    with pytest.raises(ExperimentNotFound):
        get_lineage(session, uuid.uuid4())
