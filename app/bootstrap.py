"""Composition root: build concrete adapters from settings.

The only place that knows which vendor implements which port.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor, BudgetLimits
from app.config import Settings
from app.db import make_engine, registry
from app.production.jobs import PRODUCE_SHORT, produce_short_handler
from app.production.ports import MediaGenerator
from app.production.run import ProductionDeps
from app.quality.jobs import RUN_QA, run_qa_handler
from app.quality.ports import QAGate
from app.quality.technical import TechnicalQAGate
from app.scheduling.worker import JobHandler
from app.storage import AssetStore
from integrations.fake.media import FakeMediaGenerator
from integrations.higgsfield.generator import HiggsfieldMediaGenerator
from integrations.object_storage.local import LocalAssetStore

assert registry  # every ORM model must be registered before any session is used


class ConfigurationError(Exception):
    """A required setting or credential is missing."""


@contextmanager
def open_session(settings: Settings) -> Iterator[Session]:
    """A session for one unit of CLI work; releases its connections on exit."""
    engine = make_engine(settings.database_url)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def build_asset_store(settings: Settings) -> AssetStore:
    return LocalAssetStore(settings.asset_dir)


def build_media_generator(settings: Settings) -> MediaGenerator:
    if settings.media_provider == "fake":
        # Synthetic media is free, so it must not eat into the real budget.
        return FakeMediaGenerator(cost_per_second_usd=Decimal("0"))
    if settings.higgsfield_api_key is None or settings.higgsfield_api_secret is None:
        raise ConfigurationError(
            "Higgsfield credentials are missing: set HATCH_HIGGSFIELD_API_KEY and "
            "HATCH_HIGGSFIELD_API_SECRET in .env, or use HATCH_MEDIA_PROVIDER=fake."
        )
    return HiggsfieldMediaGenerator(
        api_key=settings.higgsfield_api_key.get_secret_value(),
        api_secret=settings.higgsfield_api_secret.get_secret_value(),
    )


def build_qa_gates(settings: Settings) -> list[QAGate]:
    """Gates every generated video must go through, in order."""
    return [TechnicalQAGate()]


def build_production_deps(settings: Settings) -> ProductionDeps:
    return ProductionDeps(
        generator=build_media_generator(settings),
        store=build_asset_store(settings),
        governor=BudgetGovernor(BudgetLimits.from_settings(settings)),
        poll_interval_seconds=settings.generation_poll_interval_seconds,
        timeout_seconds=settings.generation_timeout_seconds,
    )


def build_job_handlers(settings: Settings) -> dict[str, JobHandler]:
    """Every background job type the worker can run."""
    deps = build_production_deps(settings)
    return {
        PRODUCE_SHORT: produce_short_handler(
            deps, poll_interval=timedelta(seconds=settings.generation_poll_interval_seconds)
        ),
        RUN_QA: run_qa_handler(gates=build_qa_gates(settings), store=deps.store),
    }
