"""Production allocation: exploit proven lineages, mutate promising ones,
explore something new — roughly 60 / 25 / 15.

Each new slot goes to the bucket that is furthest *below* its target share
over the IP's recent allocations. A bucket that cannot be served right now
(no proven lineage to exploit, no promising parent to mutate) is skipped and
its share flows to the others. Exploration is always available, so its share
can only go up — it can never drift to zero.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.models import AllocationBucket, DecisionType, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.ips.models import IP

# Preference when two buckets are equally far below target.
_TIE_ORDER = (AllocationBucket.EXPLORE, AllocationBucket.MUTATE, AllocationBucket.EXPLOIT)


def recent_allocations(
    session: Session, ip: IP, policy: EvolutionPolicy
) -> list[SelectionDecision]:
    """The IP's most recent experiment-creation decisions, newest first."""
    return list(
        session.scalars(
            select(SelectionDecision)
            .where(
                SelectionDecision.decision_type == DecisionType.CREATE_EXPERIMENT,
                SelectionDecision.ip_id == ip.id,
            )
            .order_by(SelectionDecision.created_at.desc(), SelectionDecision.id)
            .limit(policy.allocation_window)
        )
    )


def choose_bucket(
    session: Session, ip: IP, policy: EvolutionPolicy, *, available: set[AllocationBucket]
) -> tuple[AllocationBucket, dict[str, Any]]:
    """Pick the bucket for the next slot. Returns the bucket and the numbers
    the choice was based on, for the decision record."""
    available = available | {AllocationBucket.EXPLORE}  # exploration is always possible
    targets = {
        AllocationBucket.EXPLOIT: policy.exploit_share,
        AllocationBucket.MUTATE: policy.mutate_share,
        AllocationBucket.EXPLORE: policy.explore_share,
    }
    recent = recent_allocations(session, ip, policy)
    counts = {bucket: 0 for bucket in AllocationBucket}
    for decision in recent:
        if decision.bucket is not None:
            counts[decision.bucket] += 1
    total = len(recent)
    deficits = {bucket: targets[bucket] * (total + 1) - counts[bucket] for bucket in available}
    chosen = max(deficits, key=lambda bucket: (deficits[bucket], -_TIE_ORDER.index(bucket)))
    return chosen, {
        "targets": {bucket.value: share for bucket, share in targets.items()},
        "recent_counts": {bucket.value: count for bucket, count in counts.items()},
        "window": policy.allocation_window,
        "available": sorted(bucket.value for bucket in available),
        "deficits": {bucket.value: round(value, 4) for bucket, value in deficits.items()},
        "rule": "largest deficit against target share over the recent window",
    }
