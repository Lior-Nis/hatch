"""Lineage-aware parent selection.

Parents are chosen from evidence, deterministically:

- to *exploit*, only experiments whose hypothesis reproduced under replication
  (SUPPORTED) qualify, scored by how the whole lineage has done;
- to *mutate*, any experiment with sufficient evidence at or above the
  promising threshold qualifies, unless its hypothesis was refuted.

A lineage that already took more than its share of recent descendant slots is
passed over when an alternative exists, so production does not collapse onto
one near-identical family. The full candidate table is returned with the
choice, so a decision can be replayed from what was stored.
"""

import statistics
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.allocation import recent_allocations
from app.evolution.evidence import experiment_evidence
from app.evolution.models import AllocationBucket, ExperimentParent, ParentRelation
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import replication_descendants
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion
from app.ips.models import IP

_LINEAGE_PENALTY = 0.2
_REFUTED = (ExperimentConclusion.NOT_SUPPORTED, ExperimentConclusion.ANOMALOUS)


class ParentChoice(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    parent: Experiment
    evidence: dict[str, Any]


def _lineage_shares(session: Session, ip: IP, policy: EvolutionPolicy) -> dict[uuid.UUID, float]:
    """Each lineage's share of the IP's recent descendant slots."""
    subjects = [
        decision.subject_experiment_id
        for decision in recent_allocations(session, ip, policy)
        if decision.subject_experiment_id is not None
    ]
    if not subjects:
        return {}
    lineage_of = dict(
        session.execute(
            select(Experiment.id, Experiment.lineage_id).where(Experiment.id.in_(subjects))
        ).all()
    )
    counts: dict[uuid.UUID, int] = {}
    for subject in subjects:
        lineage = lineage_of[subject]
        counts[lineage] = counts.get(lineage, 0) + 1
    return {lineage: count / len(subjects) for lineage, count in counts.items()}


def _candidates(
    session: Session, ip: IP, policy: EvolutionPolicy, bucket: AllocationBucket
) -> list[tuple[Experiment, float]]:
    replications = set(
        session.scalars(
            select(ExperimentParent.experiment_id).where(
                ExperimentParent.relation == ParentRelation.REPLICATION
            )
        )
    )
    found = []
    for experiment in session.scalars(
        select(Experiment)
        .where(Experiment.ip_id == ip.id)
        .order_by(Experiment.created_at, Experiment.id)
    ):
        evidence = experiment_evidence(session, experiment, policy)
        if not evidence.sufficient or evidence.score is None:
            continue
        if bucket is AllocationBucket.EXPLOIT:
            if experiment.conclusion is not ExperimentConclusion.SUPPORTED:
                continue
            # How the mechanism has done across the original and its replications.
            scores = [evidence.score]
            for descendant in replication_descendants(session, experiment):
                descendant_evidence = experiment_evidence(session, descendant, policy)
                if descendant_evidence.sufficient and descendant_evidence.score is not None:
                    scores.append(descendant_evidence.score)
            found.append((experiment, statistics.median(scores)))
        else:
            if experiment.conclusion in _REFUTED or experiment.id in replications:
                continue
            if evidence.score >= policy.promising_threshold:
                found.append((experiment, evidence.score))
    return found


def select_parent(
    session: Session, ip: IP, policy: EvolutionPolicy, *, bucket: AllocationBucket
) -> ParentChoice | None:
    """The parent for the next exploit or mutate slot, or None if no
    experiment qualifies."""
    candidates = _candidates(session, ip, policy, bucket)
    if not candidates:
        return None
    shares = _lineage_shares(session, ip, policy)
    lineages = {experiment.lineage_id for experiment, _ in candidates}
    table: list[dict[str, Any]] = []
    for order, (experiment, score) in enumerate(candidates):
        share = shares.get(experiment.lineage_id, 0.0)
        over_share = share > policy.max_lineage_share and len(lineages) > 1
        table.append(
            {
                "experiment_id": str(experiment.id),
                "lineage_id": str(experiment.lineage_id),
                "score": round(score, 6),
                "lineage_share": round(share, 4),
                "adjusted_score": round(score - _LINEAGE_PENALTY * share, 6),
                "rank_order": order,
                "excluded_because": (
                    f"lineage share {share:.0%} of recent slots exceeds "
                    f"{policy.max_lineage_share:.0%}"
                    if over_share
                    else None
                ),
            }
        )
    eligible = [row for row in table if row["excluded_because"] is None] or table
    best = max(eligible, key=lambda row: (row["adjusted_score"], -row["rank_order"]))
    parent = next(e for e, _ in candidates if str(e.id) == best["experiment_id"])
    return ParentChoice(
        parent=parent,
        evidence={
            "bucket": bucket.value,
            "rule": (
                "highest adjusted score (score minus 0.2 × lineage share of recent slots) among "
                "candidates not over the lineage share cap; ties go to the older experiment"
            ),
            "candidates": table,
            "chosen": best["experiment_id"],
        },
    )
