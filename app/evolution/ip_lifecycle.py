"""IP lifecycle decisions: promote, archive, resurrect — each one audited.

    IDEA → EXPERIMENTAL → PROMISING → VALIDATED → SCALED
              ↑                                        (any active state)
              └────────────── ARCHIVED ←──────────────────────┘

Rules (thresholds live in ``EvolutionPolicy``):
- EXPERIMENTAL once the IP has experiments.
- PROMISING when IP fitness is above baseline with sufficient evidence, or a
  potential winner has appeared. This promises nothing: it only says "look".
- VALIDATED requires at least one *replicated* (SUPPORTED) hypothesis. A viral
  video alone can never get an IP here.
- SCALED requires several independently replicated lineages.
- ARCHIVED when, after enough evaluated videos, IP fitness stays below the
  archive threshold and no potential winner is still being replicated.
  Archiving changes a status and writes a decision; no history is deleted.
- An archived IP returns to EXPERIMENTAL when new evidence (for example late
  observations of already published videos) lifts its fitness, or when the
  operator says so.

Every transition goes through the IP state machine and is written as a
``SelectionDecision`` with the evidence it was based on.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.models import DecisionType, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import find_potential_winners
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion
from app.fitness.ip_aggregate import aggregate_ip_fitness
from app.ips.models import IP
from app.ips.states import IPStatus


def _facts(session: Session, ip: IP, policy: EvolutionPolicy) -> dict[str, Any]:
    fitness = aggregate_ip_fitness(session, ip)
    supported = session.scalars(
        select(Experiment.lineage_id)
        .where(Experiment.ip_id == ip.id, Experiment.conclusion == ExperimentConclusion.SUPPORTED)
        .distinct()
    ).all()
    under_replication = session.scalar(
        select(SelectionDecision.id)
        .join(Experiment, Experiment.id == SelectionDecision.subject_experiment_id)
        .where(
            SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION,
            SelectionDecision.ip_id == ip.id,
            Experiment.conclusion.is_(None),
        )
        .limit(1)
    )
    has_experiments = session.scalar(
        select(Experiment.id).where(Experiment.ip_id == ip.id).limit(1)
    )
    return {
        "ip_score": fitness.score,
        "sufficient_evidence": bool(fitness.inputs["sufficient_evidence"]),
        "videos": int(fitness.inputs["videos_considered"]),
        "ip_fitness_snapshot_id": str(fitness.id),
        "supported_lineages": len(supported),
        "supported_lineage_ids": [str(lineage) for lineage in supported],
        "potential_winners": len(find_potential_winners(session, ip, policy)),
        "under_replication": under_replication is not None,
        "has_experiments": has_experiments is not None,
    }


def _transition(
    session: Session,
    ip: IP,
    target: IPStatus,
    *,
    kind: DecisionType,
    reason: str,
    evidence: dict[str, Any],
    policy: EvolutionPolicy,
) -> SelectionDecision:
    origin = ip.status
    ip.status = target  # the IP state machine rejects illegal transitions
    decision = SelectionDecision(
        decision_type=kind,
        ip=ip,
        reason=reason,
        evidence={**evidence, "from": origin.value, "to": target.value},
        policy_version=policy.version,
    )
    session.add(decision)
    session.flush()
    return decision


def review_ip_lifecycle(
    session: Session, ip: IP, policy: EvolutionPolicy
) -> SelectionDecision | None:
    """Apply at most one lifecycle transition justified by current evidence."""
    facts = _facts(session, ip, policy)
    score, sufficient = facts["ip_score"], facts["sufficient_evidence"]
    status = ip.status

    if status is IPStatus.ARCHIVED:
        if sufficient and score >= policy.ip_resurrect_score:
            return _transition(
                session,
                ip,
                IPStatus.EXPERIMENTAL,
                kind=DecisionType.RESURRECT_IP,
                reason=(
                    f"New evidence lifts IP fitness to {score:.2f}, at or above the "
                    f"resurrection threshold {policy.ip_resurrect_score:.2f}."
                ),
                evidence=facts,
                policy=policy,
            )
        return None

    if (
        status is not IPStatus.IDEA
        and sufficient
        and facts["videos"] >= policy.ip_archive_min_videos
        and score < policy.ip_archive_score
        and not facts["under_replication"]
    ):
        return _transition(
            session,
            ip,
            IPStatus.ARCHIVED,
            kind=DecisionType.ARCHIVE_IP,
            reason=(
                f"IP fitness {score:.2f} is below the archive threshold "
                f"{policy.ip_archive_score:.2f} after {facts['videos']} evaluated videos, and "
                "no potential winner is being replicated. History is kept."
            ),
            evidence=facts,
            policy=policy,
        )

    def promote(target: IPStatus, reason: str) -> SelectionDecision:
        return _transition(
            session,
            ip,
            target,
            kind=DecisionType.PROMOTE_IP,
            reason=reason,
            evidence=facts,
            policy=policy,
        )

    above_baseline = sufficient and score >= policy.ip_promising_score
    if status is IPStatus.IDEA and facts["has_experiments"]:
        return promote(IPStatus.EXPERIMENTAL, "The IP has experiments in production.")
    if status is IPStatus.EXPERIMENTAL:
        if above_baseline:
            return promote(
                IPStatus.PROMISING,
                f"IP fitness {score:.2f} is at or above {policy.ip_promising_score:.2f} with "
                "sufficient evidence.",
            )
        if facts["potential_winners"] or facts["under_replication"]:
            return promote(
                IPStatus.PROMISING,
                "A potential winner has appeared and needs replication before it counts.",
            )
    if status is IPStatus.PROMISING and facts["supported_lineages"] >= 1:
        return promote(
            IPStatus.VALIDATED,
            f"{facts['supported_lineages']} hypothesis lineage(s) reproduced under replication.",
        )
    if (
        status is IPStatus.VALIDATED
        and facts["supported_lineages"] >= policy.ip_scaled_supported_lineages
        and above_baseline
    ):
        return promote(
            IPStatus.SCALED,
            f"{facts['supported_lineages']} independently replicated lineages and IP fitness "
            f"{score:.2f}.",
        )
    return None


def resurrect_ip(
    session: Session, ip: IP, policy: EvolutionPolicy, *, reason: str, decided_by: str
) -> SelectionDecision:
    """Operator-initiated return of an archived IP to EXPERIMENTAL."""
    facts = _facts(session, ip, policy)
    return _transition(
        session,
        ip,
        IPStatus.EXPERIMENTAL,
        kind=DecisionType.RESURRECT_IP,
        reason=reason,
        evidence={**facts, "decided_by": decided_by},
        policy=policy,
    )
