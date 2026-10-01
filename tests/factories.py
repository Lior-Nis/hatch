"""Small builders for persisted test data."""

import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor, BudgetLimits
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.production.run import ProductionDeps, produce_short
from app.quality.runner import run_quality_gates
from app.quality.technical import TechnicalQAGate
from app.storage import AssetStore
from integrations.fake.media import FakeMediaGenerator
from integrations.object_storage.local import LocalAssetStore

LIMITS = BudgetLimits(
    max_per_generation_usd=Decimal("0.75"),
    max_per_video_usd=Decimal("1.50"),
    daily_usd=Decimal("15.00"),
    monthly_usd=Decimal("500.00"),
)


def make_experiment(session: Session, *, lineage_id: uuid.UUID | None = None) -> Experiment:
    return create_experiment(session, FIRST_SHORT, lineage_id=lineage_id)


def make_generated_experiment(
    session: Session, tmp_path: Path, *, store: AssetStore | None = None
) -> Experiment:
    """An experiment whose Short has been produced by the fake generator."""
    experiment = make_experiment(session)
    deps = ProductionDeps(
        generator=FakeMediaGenerator(cost_per_second_usd=Decimal("0.05")),
        store=store or LocalAssetStore(tmp_path / "assets"),
        governor=BudgetGovernor(LIMITS),
        poll_interval_seconds=0.0,
        sleep=lambda seconds: None,
    )
    produce_short(session, experiment.id, deps)
    session.expire_all()
    return experiment


def make_reviewable_experiment(
    session: Session, tmp_path: Path, *, store: AssetStore | None = None
) -> Experiment:
    """A produced experiment that has passed automated QA and awaits a human."""
    store = store or LocalAssetStore(tmp_path / "assets")
    experiment = make_generated_experiment(session, tmp_path, store=store)
    run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=store)
    session.expire_all()
    return experiment
