"""Lineage view: one read that reconstructs why a video exists and how it was
made — hypothesis → genome → generation attempts → final asset — together with
its QA, human review, publication, and cost records.

This is a read model over immutable evidence. It never writes.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.budgets.models import LedgerStatus
from app.experiments.models import Experiment
from app.production.models import AssetKind


class ExperimentNotFound(Exception):
    pass


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class ExperimentView(_View):
    id: uuid.UUID
    lineage_id: uuid.UUID
    parent_experiment_ids: list[uuid.UUID]
    generation_reason: str
    status: str
    video_status: str
    conclusion: str | None
    output_requirements: dict[str, Any]
    created_at: datetime


class IPView(_View):
    id: uuid.UUID
    slug: str
    name: str
    category: str
    status: str
    spec: dict[str, Any]


class HypothesisView(_View):
    id: uuid.UUID
    statement: str
    rationale: str
    prediction: dict[str, Any]
    source: str
    created_at: datetime


class GenomeView(_View):
    id: uuid.UUID
    schema_version: int
    genes: dict[str, Any]
    creative_spec: str


class AttemptView(_View):
    id: uuid.UUID
    attempt_number: int
    provider: str
    model: str
    operation: str
    prompt: str
    request: dict[str, Any]
    status: str
    provider_job_id: str | None
    error: str | None
    estimated_cost_usd: Decimal | None
    actual_cost_usd: Decimal | None
    started_at: datetime | None
    finished_at: datetime | None


class AssetView(_View):
    id: uuid.UUID
    kind: str
    generation_attempt_id: uuid.UUID | None
    storage_uri: str
    sha256: str
    size_bytes: int
    mime_type: str
    media_info: dict[str, Any]
    created_at: datetime


class QAResultView(_View):
    id: uuid.UUID
    asset_id: uuid.UUID
    gate: str
    gate_version: str
    mandatory: bool
    outcome: str
    scores: dict[str, Any]
    reasons: list[str]
    created_at: datetime


class HumanReviewView(_View):
    id: uuid.UUID
    asset_id: uuid.UUID
    decision: str
    reason: str
    reviewer: str
    created_at: datetime


class PublicationView(_View):
    id: uuid.UUID
    asset_id: uuid.UUID
    platform: str
    status: str
    platform_account_id: str | None
    platform_post_id: str | None
    scheduled_at: datetime | None
    published_at: datetime | None


class LedgerView(_View):
    id: uuid.UUID
    generation_attempt_id: uuid.UUID | None
    provider: str
    model: str
    operation: str
    status: str
    estimated_cost_usd: Decimal
    actual_cost_usd: Decimal | None
    block_reason: dict[str, Any] | None
    created_at: datetime


class CostSummary(_View):
    attempts: int
    committed_usd: Decimal
    """Actual cost where the provider reported one, otherwise the estimate.
    Blocked attempts cost nothing."""


class Lineage(_View):
    experiment: ExperimentView
    ip: IPView
    hypothesis: HypothesisView
    genome: GenomeView
    generation_attempts: list[AttemptView]
    assets: list[AssetView]
    final_asset: AssetView | None
    qa_results: list[QAResultView]
    human_reviews: list[HumanReviewView]
    publications: list[PublicationView]
    ledger: list[LedgerView]
    cost: CostSummary


def get_lineage(session: Session, experiment_id: uuid.UUID) -> Lineage:
    experiment = session.scalars(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .options(
            selectinload(Experiment.ip),
            selectinload(Experiment.hypothesis),
            selectinload(Experiment.genome),
            selectinload(Experiment.generation_attempts),
            selectinload(Experiment.assets),
            selectinload(Experiment.qa_results),
            selectinload(Experiment.human_reviews),
            selectinload(Experiment.publications),
            selectinload(Experiment.ledger_entries),
        )
    ).one_or_none()
    if experiment is None:
        raise ExperimentNotFound(str(experiment_id))

    assets = [AssetView.model_validate(asset) for asset in experiment.assets]
    final_assets = [asset for asset in assets if asset.kind == AssetKind.FINAL_VIDEO.value]
    committed = sum(
        (
            entry.actual_cost_usd if entry.actual_cost_usd is not None else entry.estimated_cost_usd
            for entry in experiment.ledger_entries
            if entry.status is not LedgerStatus.BLOCKED
        ),
        Decimal("0"),
    )
    return Lineage(
        experiment=ExperimentView(
            id=experiment.id,
            lineage_id=experiment.lineage_id,
            parent_experiment_ids=[],  # parentage arrives with ExperimentParent
            generation_reason=experiment.generation_reason,
            status=experiment.status.value,
            video_status=experiment.video_status.value,
            conclusion=experiment.conclusion.value if experiment.conclusion else None,
            output_requirements=experiment.output_requirements,
            created_at=experiment.created_at,
        ),
        ip=IPView.model_validate(experiment.ip),
        hypothesis=HypothesisView.model_validate(experiment.hypothesis),
        genome=GenomeView.model_validate(experiment.genome),
        generation_attempts=[
            AttemptView.model_validate(attempt) for attempt in experiment.generation_attempts
        ],
        assets=assets,
        final_asset=final_assets[-1] if final_assets else None,
        qa_results=[QAResultView.model_validate(result) for result in experiment.qa_results],
        human_reviews=[HumanReviewView.model_validate(r) for r in experiment.human_reviews],
        publications=[PublicationView.model_validate(p) for p in experiment.publications],
        ledger=[LedgerView.model_validate(entry) for entry in experiment.ledger_entries],
        cost=CostSummary(attempts=len(experiment.generation_attempts), committed_usd=committed),
    )
