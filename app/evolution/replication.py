"""Replication before promotion.

One strong video is an observation, not proof. A high-scoring experiment
becomes a *potential* winner; Hatch then asks for controlled descendants that
keep its mechanism and tell new stories, and only their results decide:

    most descendants reproduce the effect   → SUPPORTED
    some do                                 → PARTIALLY_SUPPORTED
    none do                                 → NOT_SUPPORTED, or ANOMALOUS when
                                              the original was an outlier
    descendants never yield evidence        → INCONCLUSIVE

Only a SUPPORTED conclusion makes a lineage "proven" for exploitation and
counts towards promoting an IP. Every request and verdict is written as a
``SelectionDecision`` with the numbers it was based on.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.evidence import experiment_evidence
from app.evolution.models import (
    DecisionType,
    ExperimentParent,
    ParentRelation,
    SelectionDecision,
)
from app.evolution.policy import EvolutionPolicy
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion, VideoStatus
from app.ips.models import IP

# A descendant in one of these states will never produce audience evidence.
_DEAD_ENDS = (
    VideoStatus.GENERATION_FAILED,
    VideoStatus.QA_REJECTED,
    VideoStatus.HUMAN_REJECTED,
    VideoStatus.PUBLISH_FAILED,
    VideoStatus.ABORTED_BUDGET,
)
# Descendants made to re-test a known mechanism are not new claims.
_NOT_A_NEW_CLAIM = (ParentRelation.REPLICATION, ParentRelation.EXPLOIT)


def _replication_requests(session: Session, experiment: Experiment) -> list[SelectionDecision]:
    return list(
        session.scalars(
            select(SelectionDecision)
            .where(
                SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION,
                SelectionDecision.subject_experiment_id == experiment.id,
            )
            .order_by(SelectionDecision.created_at)
        )
    )


def replication_descendants(session: Session, experiment: Experiment) -> list[Experiment]:
    return list(
        session.scalars(
            select(Experiment)
            .join(ExperimentParent, ExperimentParent.experiment_id == Experiment.id)
            .where(
                ExperimentParent.parent_id == experiment.id,
                ExperimentParent.relation == ParentRelation.REPLICATION,
            )
            .order_by(Experiment.created_at)
        )
    )


def find_potential_winners(session: Session, ip: IP, policy: EvolutionPolicy) -> list[Experiment]:
    """Unconcluded experiments whose evidence clears the winner threshold and
    that are not already being replicated."""
    derived = set(
        session.scalars(
            select(ExperimentParent.experiment_id).where(
                ExperimentParent.relation.in_(_NOT_A_NEW_CLAIM)
            )
        )
    )
    winners = []
    for experiment in session.scalars(
        select(Experiment)
        .where(Experiment.ip_id == ip.id, Experiment.conclusion.is_(None))
        .order_by(Experiment.created_at)
    ):
        if experiment.id in derived or _replication_requests(session, experiment):
            continue
        evidence = experiment_evidence(session, experiment, policy)
        if evidence.sufficient and (evidence.score or 0) >= policy.winner_threshold:
            winners.append(experiment)
    return winners


def request_replication(
    session: Session,
    experiment: Experiment,
    policy: EvolutionPolicy,
    *,
    descendants: int | None = None,
    replacement: bool = False,
) -> SelectionDecision | None:
    """Record that ``experiment`` must be replicated. Returns None if a request
    already exists (unless this is a replacement for failed descendants)."""
    if not replacement and _replication_requests(session, experiment):
        return None
    evidence = experiment_evidence(session, experiment, policy)
    count = descendants or policy.replication_descendants
    score = evidence.score or 0.0
    reason = (
        f"Replace {count} replication descendant(s) that produced no evidence."
        if replacement
        else (
            f"Fitness {score:.2f} is at or above the potential-winner threshold "
            f"{policy.winner_threshold:.2f}. One strong video is not proof: {count} controlled "
            "descendants with the same mechanism and new stories must reproduce it."
        )
    )
    decision = SelectionDecision(
        decision_type=DecisionType.REQUEST_REPLICATION,
        ip_id=experiment.ip_id,
        subject_experiment=experiment,
        reason=reason,
        evidence={
            "score": evidence.score,
            "confidence": evidence.confidence,
            "platforms": evidence.platforms,
            "descendants_requested": count,
            "winner_threshold": policy.winner_threshold,
        },
        policy_version=policy.version,
    )
    session.add(decision)
    session.flush()
    return decision


def pending_replications(session: Session, experiment: Experiment) -> int:
    """How many requested replication descendants have not been created yet."""
    requested = sum(
        int(d.evidence["descendants_requested"]) for d in _replication_requests(session, experiment)
    )
    return max(0, requested - len(replication_descendants(session, experiment)))


def evaluate_replication(
    session: Session, experiment: Experiment, policy: EvolutionPolicy
) -> ExperimentConclusion | None:
    """Conclude a replicated experiment once its descendants have spoken.
    Returns None while the evidence is still coming in."""
    if experiment.conclusion is not None:
        return experiment.conclusion
    requests = _replication_requests(session, experiment)
    if not requests:
        return None
    requested = sum(int(d.evidence["descendants_requested"]) for d in requests)
    descendants = replication_descendants(session, experiment)
    scored = []
    dead = 0
    for descendant in descendants:
        evidence = experiment_evidence(session, descendant, policy)
        if evidence.sufficient and evidence.score is not None:
            scored.append((descendant, evidence.score))
        elif descendant.video_status in _DEAD_ENDS:
            dead += 1

    if len(scored) < policy.replication_descendants:
        if requested - len(scored) - dead > 0:
            return None  # descendants still in the pipeline or not created yet
        available = policy.replication_max_descendants - max(requested, len(descendants))
        if available > 0:
            needed = policy.replication_descendants - len(scored)
            request_replication(
                session, experiment, policy, descendants=min(needed, available), replacement=True
            )
            return None
        conclusion = ExperimentConclusion.INCONCLUSIVE
        successes = sum(1 for _, score in scored if score >= policy.support_threshold)
    else:
        successes = sum(1 for _, score in scored if score >= policy.support_threshold)
        rate = successes / len(scored)
        original = experiment_evidence(session, experiment, policy).score or 0.0
        if rate >= 2 / 3:
            conclusion = ExperimentConclusion.SUPPORTED
        elif rate >= 1 / 3:
            conclusion = ExperimentConclusion.PARTIALLY_SUPPORTED
        elif original >= policy.anomalous_threshold:
            conclusion = ExperimentConclusion.ANOMALOUS
        else:
            conclusion = ExperimentConclusion.NOT_SUPPORTED

    original_score = experiment_evidence(session, experiment, policy).score
    experiment.conclude(conclusion)
    session.add(
        SelectionDecision(
            decision_type=DecisionType.CONCLUDE_EXPERIMENT,
            ip_id=experiment.ip_id,
            subject_experiment=experiment,
            reason=(
                f"{successes} of {len(scored)} replication descendants reached "
                f"{policy.support_threshold:.2f}: {conclusion.value}."
            ),
            evidence={
                "conclusion": conclusion.value,
                "original_score": original_score,
                "support_threshold": policy.support_threshold,
                "successes": successes,
                "descendants": [
                    {"experiment_id": str(descendant.id), "score": score}
                    for descendant, score in scored
                ],
                "descendants_without_evidence": len(descendants) - len(scored),
            },
            policy_version=policy.version,
        )
    )
    session.flush()
    return conclusion
