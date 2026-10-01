"""Planner: turn evidence into the next experiments.

For each open production slot the planner picks an allocation bucket, selects
a parent when the bucket needs one, writes a ``SelectionDecision`` (reason,
evidence, bucket) and queues the creative job that will draft the candidate.
No human supplies a creative instruction anywhere in this path.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.jobs import (
    enqueue_exploit_proposal,
    enqueue_mutation_proposal,
    enqueue_novel_proposal,
)
from app.evolution.allocation import choose_bucket
from app.evolution.models import (
    AllocationBucket,
    DecisionType,
    ParentRelation,
    SelectionDecision,
)
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import replication_descendants
from app.evolution.selection import select_parent
from app.experiments.models import Experiment
from app.ips.models import IP
from app.ips.states import IPStatus
from app.scheduling.models import JobRun, JobStatus


def is_live(session: Session, decision: SelectionDecision) -> bool:
    """A creation decision still counts if its candidate exists or the job
    that will create it has not failed."""
    if decision.resulting_experiment_id is not None:
        return True
    job = session.scalars(
        select(JobRun).where(JobRun.idempotency_key == f"create_experiment:{decision.id}")
    ).first()
    return job is not None and job.status is not JobStatus.FAILED


def _open_replications(session: Session, ip: IP) -> list[tuple[Experiment, int, int]]:
    """Potential winners that still need replication descendants planned:
    ``(experiment, already_planned, requested)``, oldest request first."""
    requests = session.scalars(
        select(SelectionDecision)
        .where(
            SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION,
            SelectionDecision.ip_id == ip.id,
        )
        .order_by(SelectionDecision.created_at)
    ).all()
    requested: dict[Experiment, int] = {}
    for request in requests:
        experiment = request.subject_experiment
        if experiment is None or experiment.conclusion is not None:
            continue
        requested[experiment] = requested.get(experiment, 0) + int(
            request.evidence["descendants_requested"]
        )
    open_requests = []
    for experiment, count in requested.items():
        planned = [
            decision
            for decision in session.scalars(
                select(SelectionDecision).where(
                    SelectionDecision.decision_type == DecisionType.CREATE_EXPERIMENT,
                    SelectionDecision.subject_experiment_id == experiment.id,
                )
            )
            if decision.evidence.get("relation") == ParentRelation.REPLICATION.value
            and is_live(session, decision)
        ]
        # Descendants created outside the planner (or before it existed) count too.
        linked = {d.resulting_experiment_id for d in planned}
        unplanned = [d for d in replication_descendants(session, experiment) if d.id not in linked]
        done = len(planned) + len(unplanned)
        if done < count:
            open_requests.append((experiment, done, count))
    return open_requests


def plan_next_experiments(
    session: Session,
    ip: IP,
    policy: EvolutionPolicy,
    *,
    slots: int,
    now: datetime | None = None,
) -> list[SelectionDecision]:
    """Fill ``slots`` production slots for an IP. An archived IP gets none."""
    if ip.status is IPStatus.ARCHIVED:
        return []
    decisions = []
    for _ in range(slots):
        replications = _open_replications(session, ip)
        exploit = select_parent(session, ip, policy, bucket=AllocationBucket.EXPLOIT)
        mutate = select_parent(session, ip, policy, bucket=AllocationBucket.MUTATE)
        available = {AllocationBucket.EXPLORE}
        if exploit is not None:
            available.add(AllocationBucket.EXPLOIT)
        if replications or mutate is not None:
            available.add(AllocationBucket.MUTATE)
        bucket, allocation = choose_bucket(session, ip, policy, available=available)

        subject: Experiment | None = None
        selection = None
        relation: ParentRelation | None = None
        if bucket is AllocationBucket.EXPLOIT:
            assert exploit is not None
            subject, selection, relation = exploit.parent, exploit.evidence, ParentRelation.EXPLOIT
            reason = (
                f"Exploit: experiment {subject.id} reproduced under replication, so its "
                "mechanism gets another video with a new story."
            )
        elif bucket is AllocationBucket.MUTATE and replications:
            subject, done, requested = replications[0]
            relation = ParentRelation.REPLICATION
            reason = (
                f"Replication {done + 1} of {requested} for potential winner {subject.id}: same "
                "mechanism, new story, to test whether its result reproduces."
            )
        elif bucket is AllocationBucket.MUTATE:
            assert mutate is not None
            subject, selection, relation = mutate.parent, mutate.evidence, ParentRelation.MUTATION
            reason = (
                f"Mutate: experiment {subject.id} is promising, so a controlled change to its "
                "mechanism is tested against it."
            )
        else:
            reason = (
                "Explore: reserved novelty capacity — a new idea for this IP that extends no "
                "existing experiment."
            )

        decision = SelectionDecision(
            decision_type=DecisionType.CREATE_EXPERIMENT,
            bucket=bucket,
            ip=ip,
            subject_experiment=subject,
            reason=reason,
            evidence={
                "allocation": allocation,
                "selection": selection,
                "relation": relation.value if relation else None,
            },
            policy_version=policy.version,
        )
        session.add(decision)
        session.flush()
        if relation in (ParentRelation.EXPLOIT, ParentRelation.REPLICATION):
            assert subject is not None
            enqueue_exploit_proposal(
                session,
                subject.id,
                relation=relation,
                produce=True,
                decision_id=decision.id,
                now=now,
            )
        elif relation is ParentRelation.MUTATION:
            assert subject is not None
            enqueue_mutation_proposal(
                session, subject.id, produce=True, decision_id=decision.id, now=now
            )
        else:
            enqueue_novel_proposal(session, ip.id, produce=True, decision_id=decision.id, now=now)
        decisions.append(decision)
    session.flush()
    return decisions
