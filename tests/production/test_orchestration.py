"""The production orchestration graph: persisted steps, scenes, retries."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.experiments.states import VideoStatus
from app.production.models import (
    Asset,
    AssetKind,
    AttemptStatus,
    GenerationAttempt,
    ProductionStep,
    StepStatus,
)
from app.production.probe import probe_media
from app.production.run import ProductionDeps, ProductionError, RetryPolicy, produce_short
from app.production.storyboard import Scene, Storyboard
from app.quality.ports import QAOutcome
from app.quality.runner import run_quality_gates
from app.quality.technical import TechnicalQAGate
from integrations.fake.llm import FakeLanguageModel
from integrations.fake.media import FakeMediaGenerator
from integrations.fake.quality import FakeQAGate
from integrations.object_storage.local import LocalAssetStore
from tests.factories import LIMITS, make_experiment

FALLBACK = "lightricks/ltx-2.5/text-to-video/fast"
PLAN = {
    "routing": {
        "model": "alibaba/wan-3.0/text-to-video",
        "resolution": "480p",
        "fallbacks": [
            {"provider": "higgsfield", "model": FALLBACK, "resolution": "720p",
             "estimated_cost_usd": "0.72"},
        ],
    }
}  # fmt: skip


def deps(tmp_path: Path, generator: FakeMediaGenerator, **overrides: Any) -> ProductionDeps:
    fields: dict[str, Any] = {
        "generator": generator,
        "store": LocalAssetStore(tmp_path / "assets"),
        "governor": BudgetGovernor(LIMITS),
        "poll_interval_seconds": 0.0,
        "sleep": lambda seconds: None,
    }
    fields.update(overrides)
    return ProductionDeps(**fields)


def steps(session: Session, experiment: Experiment) -> list[ProductionStep]:
    return list(
        session.scalars(
            select(ProductionStep)
            .where(ProductionStep.experiment_id == experiment.id)
            .order_by(ProductionStep.position)
        )
    )


def attempts(session: Session, experiment: Experiment) -> list[GenerationAttempt]:
    session.expire_all()
    return list(session.get_one(Experiment, experiment.id).generation_attempts)


def multi_scene_experiment(session: Session, scenes: int, seconds: int) -> Experiment:
    genes = FIRST_SHORT.genome.genes.model_copy(
        update={"scene_count": scenes, "duration_seconds": seconds}
    )
    genome = FIRST_SHORT.genome.model_copy(update={"genes": genes})
    return create_experiment(session, FIRST_SHORT.model_copy(update={"genome": genome}))


# --- the graph ---------------------------------------------------------------


def test_single_scene_production_persists_every_step_in_order(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)

    produce_short(session, experiment.id, deps(tmp_path, FakeMediaGenerator()))

    recorded = steps(session, experiment)
    assert [(s.key, s.kind) for s in recorded] == [
        ("script", "script"),
        ("storyboard", "storyboard"),
        ("scene:1", "scene"),
        ("assemble", "assemble"),
    ]
    assert all(s.status is StepStatus.SUCCEEDED for s in recorded)
    assert all(s.started_at and s.finished_at for s in recorded)
    storyboard = recorded[1].detail["scenes"]
    assert storyboard == [
        {"index": 1, "description": FIRST_SHORT.genome.creative_spec, "duration_seconds": 8}
    ]
    assert "Nib" in recorded[2].detail["prompt"]
    assert recorded[3].detail["final_asset_id"]


def test_the_graph_is_determined_by_the_genome(session: Session) -> None:
    from app.production.run import plan_steps

    one = plan_steps(FIRST_SHORT.genome.genes)
    three = plan_steps(FIRST_SHORT.genome.genes.model_copy(update={"scene_count": 3}))

    assert [key for key, _ in one] == ["script", "storyboard", "scene:1", "assemble"]
    assert [key for key, _ in three] == [
        "script", "storyboard", "scene:1", "scene:2", "scene:3", "assemble",
    ]  # fmt: skip
    assert plan_steps(FIRST_SHORT.genome.genes) == one


def test_multi_scene_production_generates_each_scene_and_assembles_them(
    session: Session, tmp_path: Path
) -> None:
    experiment = multi_scene_experiment(session, scenes=3, seconds=8)
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))
    board = Storyboard(
        scenes=[
            Scene(index=1, description="A glow pulses behind a fern.", duration_seconds=3),
            Scene(
                index=2, description="Nib tiptoes closer and parts the fern.", duration_seconds=3
            ),
            Scene(index=3, description="Fireflies rise and light the hollow.", duration_seconds=2),
        ]
    )
    llm = FakeLanguageModel([board])

    result = produce_short(session, experiment.id, deps(tmp_path, generator, llm=llm))

    assert result.video_status is VideoStatus.GENERATED
    assert [r.duration_seconds for r in generator.submitted_requests] == [3, 3, 2]
    assert "parts the fern" in generator.submitted_requests[1].prompt
    assets = session.scalars(select(Asset).order_by(Asset.created_at)).all()
    assert [a.kind for a in assets] == [AssetKind.RAW_VIDEO] * 3 + [AssetKind.FINAL_VIDEO]
    final = assets[-1]
    assert final.id == result.asset_id
    info = probe_media(deps(tmp_path, generator).store.local_path(final.storage_uri))
    assert info.duration_seconds == pytest.approx(8.0, abs=0.5)
    assert (info.width, info.height) == (1080, 1920)
    assert info.has_audio
    assert llm.requests[0].purpose == "storyboard"
    assert BudgetGovernor(LIMITS).committed_spend_for_experiment(session, experiment.id) == Decimal(
        "0.40"
    )


def test_a_storyboard_that_does_not_match_the_genome_is_rejected(
    session: Session, tmp_path: Path
) -> None:
    experiment = multi_scene_experiment(session, scenes=3, seconds=8)
    wrong = Storyboard(scenes=[Scene(index=1, description="Only one.", duration_seconds=8)])
    generator = FakeMediaGenerator()

    with pytest.raises(ProductionError, match="3 scenes"):
        produce_short(
            session, experiment.id, deps(tmp_path, generator, llm=FakeLanguageModel([wrong]))
        )

    assert generator.submitted_requests == []
    assert steps(session, experiment)[1].status is StepStatus.FAILED


def test_multi_scene_production_without_a_language_model_fails_clearly(
    session: Session, tmp_path: Path
) -> None:
    experiment = multi_scene_experiment(session, scenes=2, seconds=8)

    with pytest.raises(ProductionError, match="language model"):
        produce_short(session, experiment.id, deps(tmp_path, FakeMediaGenerator()))


# --- retries -----------------------------------------------------------------


def test_failed_generation_is_retried_with_a_repaired_prompt(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(fail_next=["nsfw: flagged by provider"])

    result = produce_short(session, experiment.id, deps(tmp_path, generator))

    assert result.video_status is VideoStatus.GENERATED
    first, second = attempts(session, experiment)
    assert (first.status, first.strategy) == (AttemptStatus.FAILED, "original")
    assert (second.status, second.strategy) == (AttemptStatus.SUCCEEDED, "prompt_repair")
    assert second.model == first.model
    assert second.prompt != first.prompt
    assert second.prompt.startswith(first.prompt)
    assert "family-friendly" in second.prompt


def test_the_third_attempt_falls_back_to_another_model(session: Session, tmp_path: Path) -> None:
    experiment = create_experiment_with_plan(session)
    generator = FakeMediaGenerator(fail_next=["timeout", "timeout"])

    result = produce_short(session, experiment.id, deps(tmp_path, generator))

    assert result.video_status is VideoStatus.GENERATED
    recorded = attempts(session, experiment)
    assert [a.strategy for a in recorded] == ["original", "prompt_repair", "fallback_model"]
    assert recorded[2].model == FALLBACK
    assert recorded[2].request["resolution"] == "720p"
    assert recorded[2].status is AttemptStatus.SUCCEEDED


def create_experiment_with_plan(session: Session) -> Experiment:
    from app.experiments.service import get_or_create_ip, persist_candidate

    return persist_candidate(
        session,
        ip=get_or_create_ip(session, FIRST_SHORT.ip),
        hypothesis=FIRST_SHORT.hypothesis,
        genome=FIRST_SHORT.genome,
        output=FIRST_SHORT.output,
        generation_reason="test",
        production_plan=PLAN,
    )


def test_retries_stop_after_the_last_attempt_and_every_attempt_is_recorded(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(fail_next=["boom 1", "boom 2", "boom 3", "never reached"])

    result = produce_short(session, experiment.id, deps(tmp_path, generator))

    assert result.video_status is VideoStatus.GENERATION_FAILED
    assert result.error == "boom 3"
    recorded = attempts(session, experiment)
    assert [a.error for a in recorded] == ["boom 1", "boom 2", "boom 3"]
    assert [a.attempt_number for a in recorded] == [1, 2, 3]
    assert steps(session, experiment)[2].status is StepStatus.FAILED
    assert session.scalars(select(Asset)).all() == []


def test_retries_stop_cleanly_when_the_video_budget_is_exhausted(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(
        cost_per_second_usd=Decimal("0.075"),  # $0.60 per 8s attempt
        fail_next=["boom 1", "boom 2", "boom 3"],
        charge_failures=True,
    )

    result = produce_short(session, experiment.id, deps(tmp_path, generator))

    assert result.video_status is VideoStatus.ABORTED_BUDGET
    recorded = attempts(session, experiment)
    assert [a.status for a in recorded] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
        AttemptStatus.BLOCKED_BUDGET,
    ]
    assert len(generator.submitted_requests) == 2  # the third was never sent
    ledger = session.scalars(select(BudgetLedgerEntry).order_by(BudgetLedgerEntry.created_at)).all()
    assert [e.status for e in ledger] == [
        LedgerStatus.SETTLED,
        LedgerStatus.SETTLED,
        LedgerStatus.BLOCKED,
    ]
    assert BudgetGovernor(LIMITS).committed_spend_for_experiment(session, experiment.id) == Decimal(
        "1.20"
    )


def test_retry_policy_can_disable_retries(session: Session, tmp_path: Path) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator(fail_next=["boom"])

    result = produce_short(
        session,
        experiment.id,
        deps(tmp_path, generator, retry=RetryPolicy(max_attempts_per_scene=1)),
    )

    assert result.video_status is VideoStatus.GENERATION_FAILED
    assert len(attempts(session, experiment)) == 1


# --- regeneration after QA rejection -------------------------------------------


def test_a_qa_rejected_video_is_regenerated_once_with_the_reasons(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator()
    production = deps(tmp_path, generator)
    produce_short(session, experiment.id, production)
    blurry = FakeQAGate(
        name="visual", mandatory=True, outcome=QAOutcome.FAIL, reasons=["character drifts"]
    )
    run_quality_gates(session, experiment.id, gates=[blurry], store=production.store)

    result = produce_short(session, experiment.id, production)

    assert result.video_status is VideoStatus.GENERATED
    first, second = attempts(session, experiment)
    assert second.strategy == "qa_repair"
    assert "character drifts" in second.prompt
    finals = session.scalars(select(Asset).where(Asset.kind == AssetKind.FINAL_VIDEO)).all()
    assert len(finals) == 2
    assert result.asset_id != first.id
    run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=production.store)
    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING


def test_regeneration_after_qa_rejection_is_capped(session: Session, tmp_path: Path) -> None:
    experiment = make_experiment(session)
    generator = FakeMediaGenerator()
    production = deps(tmp_path, generator)
    reject = FakeQAGate(name="visual", mandatory=True, outcome=QAOutcome.FAIL, reasons=["bad"])
    produce_short(session, experiment.id, production)
    run_quality_gates(session, experiment.id, gates=[reject], store=production.store)
    produce_short(session, experiment.id, production)
    run_quality_gates(session, experiment.id, gates=[reject], store=production.store)

    with pytest.raises(ValueError, match="qa_rejected"):
        produce_short(session, experiment.id, production)

    assert len(generator.submitted_requests) == 2
