"""Fitness evaluation over stored observations.

A fitness value is always derived from one metric snapshot of one publication
and stored with the inputs and formula version that produced it, so it can be
audited and recomputed. Baselines are per platform account: signals from
different platforms are never pooled.
"""

import logging
import statistics
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.ingestion import EARLY_EVALUATION_CHECKPOINT, FINAL_CHECKPOINT
from app.analytics.models import MetricSnapshot
from app.analytics.ports import NormalizedMetrics
from app.budgets.models import BudgetLedgerEntry, committed_usd, is_spend
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.fitness.heuristic import InsufficientSignals
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.fitness.ports import FitnessEvaluator, FitnessInputs
from app.publishing.models import Publication, PublicationRecordStatus

logger = logging.getLogger(__name__)

MIN_BASELINE_VIDEOS = 3


def account_baseline(
    session: Session, publication: Publication, checkpoint: str | None
) -> NormalizedMetrics | None:
    """Median of each signal over the other videos published on the same
    platform account, observed at the same maturity checkpoint. None until the
    account has enough history."""
    others = session.scalars(
        select(MetricSnapshot)
        .join(Publication, Publication.id == MetricSnapshot.publication_id)
        .where(
            Publication.platform_account_id == publication.platform_account_id,
            Publication.experiment_id != publication.experiment_id,
            Publication.status == PublicationRecordStatus.PUBLISHED,
            MetricSnapshot.checkpoint == checkpoint,
        )
        .order_by(MetricSnapshot.observed_at)
    ).all()
    latest = {snapshot.publication_id: snapshot for snapshot in others}  # last one wins
    if len(latest) < MIN_BASELINE_VIDEOS:
        return None
    values: dict[str, float] = {}
    for name in NormalizedMetrics.model_fields:
        reported = [
            s.normalized[name] for s in latest.values() if s.normalized.get(name) is not None
        ]
        if reported:
            values[name] = statistics.median(reported)
    integers = {
        name
        for name, field in NormalizedMetrics.model_fields.items()
        if field.annotation == (int | None)
    }
    return NormalizedMetrics.model_validate(
        {name: round(value) if name in integers else value for name, value in values.items()}
    )


def production_cost(session: Session, experiment_id: uuid.UUID) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(committed_usd()), 0)).where(
            is_spend(), BudgetLedgerEntry.experiment_id == experiment_id
        )
    )
    return Decimal(total or 0)


def evaluate_snapshot(
    session: Session,
    snapshot_id: uuid.UUID,
    evaluator: FitnessEvaluator,
    *,
    target_cost_usd: Decimal | None,
    strict: bool = False,
) -> FitnessSnapshot | None:
    """Compute and store the video fitness for one metric snapshot. Returns
    None when the evidence is too thin to score (``strict`` raises instead).
    Idempotent per snapshot and formula version."""
    snapshot = session.get_one(MetricSnapshot, snapshot_id)
    existing = session.scalars(
        select(FitnessSnapshot).where(
            FitnessSnapshot.metric_snapshot_id == snapshot.id,
            FitnessSnapshot.evaluator == evaluator.name,
            FitnessSnapshot.evaluator_version == evaluator.version,
        )
    ).first()
    if existing is not None:
        return existing

    publication = snapshot.publication
    experiment = publication.experiment
    duration = publication.asset.media_info.get("duration_seconds")
    inputs = FitnessInputs(
        platform=publication.platform,
        metrics=NormalizedMetrics.model_validate(snapshot.normalized),
        hours_since_publication=snapshot.hours_since_publication,
        production_cost_usd=production_cost(session, experiment.id),
        target_cost_usd=target_cost_usd,
        duration_seconds=float(duration) if duration else None,
        observed_at=snapshot.observed_at,
        baseline=account_baseline(session, publication, snapshot.checkpoint),
    )
    try:
        result = evaluator.evaluate(inputs)
    except InsufficientSignals as exc:
        logger.info(
            "fitness_not_scored",
            extra={"experiment_id": str(experiment.id), "reason": str(exc)},
        )
        if strict:
            raise
        return None
    fitness = FitnessSnapshot(
        scope=FitnessScope.VIDEO,
        ip_id=experiment.ip_id,
        experiment=experiment,
        publication=publication,
        metric_snapshot=snapshot,
        platform=publication.platform,
        checkpoint=snapshot.checkpoint,
        evaluator=result.evaluator,
        evaluator_version=result.evaluator_version,
        score=result.score,
        components=result.components,
        inputs={
            **result.inputs.model_dump(mode="json"),
            "confidence": result.confidence,
            "details": result.details,
        },
    )
    session.add(fitness)
    session.flush()
    return fitness


def advance_for_checkpoint(experiment: Experiment, checkpoint: str | None) -> None:
    """Move the experiment along its lifecycle as maturity checkpoints pass.
    The checkpoint passes whether or not the evidence was strong enough to
    score: thin evidence is itself a result."""
    if checkpoint is None:
        return
    if experiment.status is ExperimentStatus.OBSERVING:
        experiment.status = ExperimentStatus.EARLY_EVALUATED
    if checkpoint == EARLY_EVALUATION_CHECKPOINT:
        return
    if checkpoint == FINAL_CHECKPOINT:
        if experiment.status is ExperimentStatus.EARLY_EVALUATED:
            experiment.status = ExperimentStatus.MATURED
        if experiment.video_status is VideoStatus.OBSERVING:
            experiment.video_status = VideoStatus.EVALUATED
