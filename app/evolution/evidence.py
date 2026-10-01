"""Experiment-level evidence: one experiment's fitness across platforms.

Each platform's value is that video's most mature fitness on that platform
(already relative to the platform account's own baseline). Platforms are then
combined with fixed weights scaled by confidence — raw platform signals are
never mixed.
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.ingestion import CHECKPOINTS
from app.evolution.policy import EvolutionPolicy
from app.experiments.models import Experiment
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.platforms import Platform

_MATURITY = {checkpoint: rank for rank, checkpoint in enumerate(CHECKPOINTS)}


class ExperimentEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float | None
    confidence: float
    platforms: dict[str, float]
    sufficient: bool


def experiment_evidence(
    session: Session, experiment: Experiment, policy: EvolutionPolicy | None = None
) -> ExperimentEvidence:
    policy = policy or EvolutionPolicy()
    best: dict[Platform, FitnessSnapshot] = {}
    for row in session.scalars(
        select(FitnessSnapshot).where(
            FitnessSnapshot.scope == FitnessScope.VIDEO,
            FitnessSnapshot.experiment_id == experiment.id,
        )
    ):
        if row.platform is None:
            continue
        rank = (_MATURITY.get(row.checkpoint or "", -1), row.created_at)
        current = best.get(row.platform)
        if current is None or rank > (
            _MATURITY.get(current.checkpoint or "", -1),
            current.created_at,
        ):
            best[row.platform] = row
    if not best:
        return ExperimentEvidence(score=None, confidence=0.0, platforms={}, sufficient=False)

    weighted = total = 0.0
    confidence = 0.0
    for platform, row in best.items():
        row_confidence = float(row.inputs.get("confidence", 1.0))
        weight = policy.platform_weights[platform] * max(row_confidence, 0.01)
        weighted += row.score * weight
        total += weight
        confidence = max(confidence, row_confidence)
    return ExperimentEvidence(
        score=weighted / total,
        confidence=confidence,
        platforms={platform.value: row.score for platform, row in best.items()},
        sufficient=confidence >= policy.min_confidence,
    )
