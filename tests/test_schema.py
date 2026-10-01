from datetime import UTC, datetime
from decimal import Decimal

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from app.budgets.models import BudgetLedgerEntry
from app.db import Base
from app.experiments.models import CreativeGenome, Experiment, Hypothesis
from app.experiments.states import ExperimentStatus, VideoStatus
from app.ips.models import IP
from app.ips.states import IPStatus
from app.platforms import Platform
from app.production.models import Asset, AssetKind, AttemptStatus, GenerationAttempt
from app.publishing.models import Publication, PublicationRecordStatus
from app.quality.models import QAResult
from app.quality.ports import QAOutcome


def test_migrations_produce_exactly_the_schema_the_models_declare(connection: Connection) -> None:
    context = MigrationContext.configure(connection, opts={"compare_type": True})

    assert compare_metadata(context, Base.metadata) == []


def test_a_complete_experiment_can_be_persisted_and_reloaded(session: Session) -> None:
    ip = IP(slug="pip-the-snail", name="Pip the Snail", category="narrative_adventure", spec={})
    hypothesis = Hypothesis(
        ip=ip,
        statement="Question hooks raise completion for Pip stories.",
        rationale="Curiosity gap in the first two seconds.",
        prediction={"metric": "completion_rate", "direction": "increase"},
        source="fixture",
    )
    genome = CreativeGenome(
        schema_version=1,
        genes={"hook_type": "question", "duration_seconds": 8},
        creative_spec="Pip wonders where rainbows come from.",
    )
    experiment = Experiment(
        ip=ip, hypothesis=hypothesis, genome=genome, generation_reason="vertical-slice fixture"
    )
    attempt = GenerationAttempt(
        experiment=experiment,
        attempt_number=1,
        idempotency_key="exp:1",
        provider="fake",
        model="fake-video-1",
        operation="text_to_video",
        prompt="Pip the snail looks at a rainbow.",
        request={"duration_seconds": 8},
        status=AttemptStatus.SUCCEEDED,
        estimated_cost_usd=Decimal("0.40"),
        actual_cost_usd=Decimal("0.38"),
        started_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 10, 1, 12, 3, tzinfo=UTC),
    )
    asset = Asset(
        experiment=experiment,
        generation_attempt=attempt,
        kind=AssetKind.FINAL_VIDEO,
        storage_uri="file:///var/assets/short.mp4",
        sha256="a" * 64,
        size_bytes=1234,
        mime_type="video/mp4",
        media_info={"width": 1080, "height": 1920},
    )
    qa = QAResult(
        experiment=experiment,
        asset=asset,
        gate="technical",
        gate_version="1",
        mandatory=True,
        outcome=QAOutcome.PASS,
        scores={},
        reasons=[],
    )
    publication = Publication(
        experiment=experiment,
        asset=asset,
        platform=Platform.YOUTUBE_SHORTS,
        status=PublicationRecordStatus.PLANNED,
    )
    ledger = BudgetLedgerEntry(
        provider="fake",
        model="fake-video-1",
        operation="text_to_video",
        experiment=experiment,
        generation_attempt=attempt,
        estimated_cost_usd=Decimal("0.40"),
        actual_cost_usd=Decimal("0.38"),
    )
    session.add_all([experiment, attempt, asset, qa, publication, ledger])
    session.flush()
    experiment_id = experiment.id
    session.expunge_all()

    loaded = session.scalars(select(Experiment).where(Experiment.id == experiment_id)).one()

    assert loaded.status is ExperimentStatus.HYPOTHESIS_CREATED
    assert loaded.video_status is VideoStatus.PROPOSED
    assert loaded.lineage_id is not None
    assert loaded.ip.status is IPStatus.IDEA
    assert loaded.hypothesis.prediction["metric"] == "completion_rate"
    assert loaded.genome.genes["hook_type"] == "question"
    assert loaded.created_at.tzinfo is not None
    [loaded_attempt] = loaded.generation_attempts
    assert loaded_attempt.actual_cost_usd == Decimal("0.38")
    [loaded_asset] = loaded.assets
    assert loaded_asset.generation_attempt_id == loaded_attempt.id
    assert loaded_asset.media_info["height"] == 1920
    assert [r.outcome for r in loaded.qa_results] == [QAOutcome.PASS]
    assert [p.platform for p in loaded.publications] == [Platform.YOUTUBE_SHORTS]
    [loaded_ledger] = loaded.ledger_entries
    assert loaded_ledger.estimated_cost_usd == Decimal("0.40")
    assert loaded_ledger.generation_attempt_id == loaded_attempt.id
