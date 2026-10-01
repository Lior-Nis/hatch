"""One autonomous evolution cycle for an IP:

    evidence → verdicts → replication requests → IP lifecycle → next experiments

This is the step that closes the loop: observed performance changes what gets
created next, and every choice is written down with its reasons.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.evidence import experiment_evidence
from app.evolution.ip_lifecycle import review_ip_lifecycle
from app.evolution.models import DecisionType, SelectionDecision
from app.evolution.planner import is_live, plan_next_experiments
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import (
    evaluate_replication,
    find_potential_winners,
    request_replication,
)
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion, ExperimentStatus, VideoStatus
from app.ips.models import IP
from app.knowledge.synthesis import synthesize_ip_knowledge
from app.scheduling.models import JobRun, JobStatus

# A video in one of these states is on its way to being published.
_IN_PIPELINE = (
    VideoStatus.PROPOSED,
    VideoStatus.SCRIPTED,
    VideoStatus.STORYBOARDED,
    VideoStatus.GENERATING,
    VideoStatus.GENERATED,
    VideoStatus.QA_PENDING,
    VideoStatus.APPROVAL_PENDING,
    VideoStatus.READY,
    VideoStatus.SCHEDULED,
)
_DEAD_ENDS = (
    VideoStatus.GENERATION_FAILED,
    VideoStatus.QA_REJECTED,
    VideoStatus.HUMAN_REJECTED,
    VideoStatus.PUBLISH_FAILED,
    VideoStatus.ABORTED_BUDGET,
)
_MAX_LIFECYCLE_STEPS = 4


@dataclass(frozen=True)
class EvolutionReport:
    conclusions: int
    replication_requests: int
    lifecycle_transitions: int
    planned: int
    in_pipeline: int
    knowledge_updates: int = 0


def _has_live_job(session: Session, experiment: Experiment) -> bool:
    return (
        session.scalars(
            select(JobRun.id).where(
                JobRun.experiment_id == experiment.id,
                JobRun.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
        ).first()
        is not None
    )


def _conclude(
    session: Session,
    experiment: Experiment,
    conclusion: ExperimentConclusion,
    policy: EvolutionPolicy,
    *,
    reason: str,
    score: float | None,
) -> None:
    experiment.conclude(conclusion)
    session.add(
        SelectionDecision(
            decision_type=DecisionType.CONCLUDE_EXPERIMENT,
            ip_id=experiment.ip_id,
            subject_experiment=experiment,
            reason=reason,
            evidence={"conclusion": conclusion.value, "score": score, "replicated": False},
            policy_version=policy.version,
        )
    )


def conclude_finished_experiments(session: Session, ip: IP, policy: EvolutionPolicy) -> int:
    """Close experiments that have said all they will say without replication:
    those that can no longer produce evidence, and matured ones that never
    reached the potential-winner bar."""
    under_replication = set(
        session.scalars(
            select(SelectionDecision.subject_experiment_id).where(
                SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION,
                SelectionDecision.ip_id == ip.id,
            )
        )
    )
    concluded = 0
    for experiment in session.scalars(
        select(Experiment).where(Experiment.ip_id == ip.id, Experiment.conclusion.is_(None))
    ):
        if experiment.id in under_replication:
            continue
        if experiment.video_status in _DEAD_ENDS and not _has_live_job(session, experiment):
            _conclude(
                session,
                experiment,
                ExperimentConclusion.INCONCLUSIVE,
                policy,
                reason=(
                    f"The video ended as {experiment.video_status.value} and was never "
                    "observed: no audience evidence for or against the hypothesis."
                ),
                score=None,
            )
            concluded += 1
        elif experiment.status is ExperimentStatus.MATURED:
            evidence = experiment_evidence(session, experiment, policy)
            score = evidence.score if evidence.sufficient else None
            if score is not None and score >= policy.winner_threshold:
                continue  # a potential winner: replication decides, not this rule
            if score is not None and score < policy.promising_threshold:
                conclusion = ExperimentConclusion.NOT_SUPPORTED
                reason = (
                    f"Matured at fitness {score:.2f}, below the account baseline "
                    f"({policy.promising_threshold:.2f}): the predicted uplift did not appear."
                )
            else:
                conclusion = ExperimentConclusion.INCONCLUSIVE
                shown = f"{score:.2f}" if score is not None else "too little evidence"
                reason = (
                    f"Matured at fitness {shown}: not a failure, but below the "
                    f"potential-winner bar ({policy.winner_threshold:.2f}) and unreplicated."
                )
            _conclude(session, experiment, conclusion, policy, reason=reason, score=score)
            concluded += 1
    session.flush()
    return concluded


def pipeline_size(session: Session, ip: IP) -> int:
    """Videos on their way to publication, plus candidates already ordered
    from the creative agent but not created yet."""
    candidates = session.scalars(
        select(Experiment.id).where(
            Experiment.ip_id == ip.id,
            Experiment.conclusion.is_(None),
            Experiment.video_status.in_(_IN_PIPELINE),
        )
    ).all()
    ordered = [
        decision
        for decision in session.scalars(
            select(SelectionDecision).where(
                SelectionDecision.decision_type == DecisionType.CREATE_EXPERIMENT,
                SelectionDecision.ip_id == ip.id,
                SelectionDecision.resulting_experiment_id.is_(None),
            )
        )
        if is_live(session, decision)
    ]
    return len(candidates) + len(ordered)


def evolve_ip(
    session: Session,
    ip: IP,
    policy: EvolutionPolicy,
    *,
    pipeline_target: int,
    now: datetime | None = None,
) -> EvolutionReport:
    conclusions = 0
    # 1. Verdicts for experiments whose replication descendants have reported.
    for experiment_id in set(
        session.scalars(
            select(SelectionDecision.subject_experiment_id).where(
                SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION,
                SelectionDecision.ip_id == ip.id,
            )
        )
    ):
        experiment = session.get_one(Experiment, experiment_id)
        if experiment.conclusion is None and evaluate_replication(session, experiment, policy):
            conclusions += 1
    # 2. Close experiments that will say nothing more.
    conclusions += conclude_finished_experiments(session, ip, policy)
    # 3. New potential winners must be replicated before they count.
    requests = sum(
        1
        for winner in find_potential_winners(session, ip, policy)
        if request_replication(session, winner, policy) is not None
    )
    # 4. The IP's own lifecycle, one audited step at a time.
    transitions = 0
    for _ in range(_MAX_LIFECYCLE_STEPS):
        if review_ip_lifecycle(session, ip, policy) is None:
            break
        transitions += 1
    # 5. Refresh what the creative agent will read: derived knowledge.
    knowledge = synthesize_ip_knowledge(session, ip, policy)
    # 6. Top the production pipeline up to its target.
    in_pipeline = pipeline_size(session, ip)
    planned = plan_next_experiments(
        session, ip, policy, slots=max(0, pipeline_target - in_pipeline), now=now
    )
    session.commit()
    return EvolutionReport(
        conclusions=conclusions,
        replication_requests=requests,
        lifecycle_transitions=transitions,
        planned=len(planned),
        in_pipeline=in_pipeline,
        knowledge_updates=len(knowledge),
    )
