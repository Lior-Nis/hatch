from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.jobs import (
    PROPOSE_EXPLOIT,
    PROPOSE_MUTATION,
    PROPOSE_NOVEL,
    creative_handlers,
)
from app.evolution.cycle import evolve_ip
from app.evolution.jobs import EVOLVE_IP, evolution_handlers
from app.evolution.models import AllocationBucket, DecisionType, SelectionDecision
from app.evolution.planner import plan_next_experiments
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import evaluate_replication, request_replication
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion, ExperimentStatus, VideoStatus
from app.ips.models import IP
from app.ips.states import IPStatus
from app.production.jobs import PRODUCE_SHORT
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.recurring import ensure_recurring
from app.scheduling.worker import Worker
from integrations.fake.llm import FakeLanguageModel
from tests.creative.test_hypotheses import DEFAULTS, exploit_draft, novel_draft
from tests.evolution.helpers import evaluated
from tests.factories import LIMITS, force_video_status, make_experiment

POLICY = EvolutionPolicy()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def creations(session: Session) -> list[SelectionDecision]:
    return list(
        session.scalars(
            select(SelectionDecision)
            .where(SelectionDecision.decision_type == DecisionType.CREATE_EXPERIMENT)
            .order_by(SelectionDecision.created_at)
        )
    )


def jobs(session: Session, job_type: str) -> list[JobRun]:
    return list(session.scalars(select(JobRun).where(JobRun.job_type == job_type)))


def supported_lineage(session: Session) -> Experiment:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    for _ in range(3):
        evaluated(session, 0.7, parent=winner)
    evaluate_replication(session, winner, POLICY)
    return winner


def empty_ip(session: Session) -> IP:
    from app.experiments.service import get_or_create_ip

    return get_or_create_ip(session, FIRST_SHORT.ip)


# --- planning -----------------------------------------------------------------------


def test_cold_start_plans_exploration_with_reason_evidence_and_bucket(session: Session) -> None:
    ip = empty_ip(session)

    planned = plan_next_experiments(session, ip, POLICY, slots=2, now=NOW)

    assert [d.bucket for d in planned] == [AllocationBucket.EXPLORE] * 2
    for decision in planned:
        assert decision.decision_type is DecisionType.CREATE_EXPERIMENT
        assert decision.ip_id == ip.id and decision.subject_experiment_id is None
        assert "explor" in decision.reason.lower()
        assert decision.evidence["allocation"]["recent_counts"] is not None
        assert decision.policy_version == POLICY.version
    queued = jobs(session, PROPOSE_NOVEL)
    assert len(queued) == 2
    assert {job.payload["decision_id"] for job in queued} == {str(d.id) for d in planned}
    assert all(job.payload["produce"] is True for job in queued)


def test_with_evidence_slots_are_split_between_exploit_mutate_and_explore(session: Session) -> None:
    proven = supported_lineage(session)
    evaluated(session, 0.6)  # promising, unreplicated

    planned = plan_next_experiments(session, proven.ip, POLICY, slots=20, now=NOW)

    counts = Counter(decision.bucket for decision in planned)
    assert counts == {
        AllocationBucket.EXPLOIT: 12,
        AllocationBucket.MUTATE: 5,
        AllocationBucket.EXPLORE: 3,
    }
    exploit = next(d for d in planned if d.bucket is AllocationBucket.EXPLOIT)
    assert exploit.subject_experiment_id == proven.id
    assert exploit.evidence["selection"]["chosen"] == str(proven.id)
    assert len(jobs(session, PROPOSE_EXPLOIT)) == 12
    assert len(jobs(session, PROPOSE_MUTATION)) == 5
    assert len(jobs(session, PROPOSE_NOVEL)) == 3


def test_pending_replications_are_planned_first_and_never_over_planned(session: Session) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)

    planned = plan_next_experiments(session, winner.ip, POLICY, slots=10, now=NOW)
    again = plan_next_experiments(session, winner.ip, POLICY, slots=10, now=NOW)

    replications = [d for d in planned + again if d.evidence.get("relation") == "replication"]
    assert len(replications) == 3
    assert all(d.subject_experiment_id == winner.id for d in replications)
    assert all(d.bucket is AllocationBucket.MUTATE for d in replications)
    payloads = [job.payload for job in jobs(session, PROPOSE_EXPLOIT)]
    assert sum(1 for payload in payloads if payload["relation"] == "replication") == 3
    assert not any(d.bucket is AllocationBucket.EXPLOIT for d in planned + again)


def test_an_archived_ip_gets_no_production(session: Session) -> None:
    ip = evaluated(session, 0.6).ip
    ip.status = IPStatus.ARCHIVED

    assert plan_next_experiments(session, ip, POLICY, slots=5, now=NOW) == []
    assert creations(session) == []


# --- decisions become experiments --------------------------------------------------------


def creative_worker(session: Session, llm: FakeLanguageModel) -> Worker:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = creative_handlers(
        llm=llm, governor=BudgetGovernor(LIMITS), output=FIRST_SHORT.output, production=DEFAULTS
    )
    return Worker(sessions, handlers, worker_id="w1", clock=lambda: NOW + timedelta(minutes=1))


def test_the_created_candidate_is_linked_back_to_its_decision(session: Session) -> None:
    ip = make_experiment(session).ip
    [decision] = plan_next_experiments(session, ip, POLICY, slots=1, now=NOW)

    creative_worker(session, FakeLanguageModel([novel_draft()])).run_once()

    session.expire_all()
    stored = session.get_one(SelectionDecision, decision.id)
    assert stored.resulting_experiment_id is not None
    candidate = session.get_one(Experiment, stored.resulting_experiment_id)
    assert candidate.video_status is VideoStatus.PROPOSED
    [production] = jobs(session, PRODUCE_SHORT)
    assert production.experiment_id == candidate.id


def test_replication_job_creates_a_descendant_marked_as_replication(session: Session) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    plan_next_experiments(session, winner.ip, POLICY, slots=1, now=NOW)

    creative_worker(session, FakeLanguageModel([exploit_draft()])).run_once()

    session.expire_all()
    [link] = session.get_one(Experiment, winner.id).children
    assert link.relation.value == "replication"


# --- the autonomous cycle -------------------------------------------------------------------


def test_observed_performance_leads_to_the_next_experiments_without_instructions(
    session: Session,
) -> None:
    strong = evaluated(session, 0.9)
    evaluated(session, 0.5)

    report = evolve_ip(session, strong.ip, POLICY, pipeline_target=3, now=NOW)

    assert report.replication_requests == 1
    assert strong.ip.status is IPStatus.PROMISING
    planned = creations(session)
    assert len(planned) == report.planned == 3
    assert any(
        d.subject_experiment_id == strong.id and d.evidence["relation"] == "replication"
        for d in planned
    )
    assert not any(d.bucket is AllocationBucket.EXPLOIT for d in planned)  # nothing proven yet
    assert all(d.reason and d.evidence and d.bucket for d in planned)


def test_a_reproduced_winner_is_promoted_and_then_exploited(session: Session) -> None:
    strong = evaluated(session, 0.9)
    evolve_ip(session, strong.ip, POLICY, pipeline_target=0, now=NOW)
    for _ in range(3):
        evaluated(session, 0.7, parent=strong)  # the replication descendants report in

    report = evolve_ip(session, strong.ip, POLICY, pipeline_target=4, now=NOW)

    assert strong.conclusion is ExperimentConclusion.SUPPORTED
    assert report.conclusions == 1
    assert strong.ip.status is IPStatus.VALIDATED
    exploits = [d for d in creations(session) if d.bucket is AllocationBucket.EXPLOIT]
    assert exploits and all(d.subject_experiment_id == strong.id for d in exploits)


def test_the_pipeline_is_only_topped_up_to_its_target(session: Session) -> None:
    ip = make_experiment(session).ip  # one candidate already waiting to be produced
    make_experiment(session)

    report = evolve_ip(session, ip, POLICY, pipeline_target=3, now=NOW)
    again = evolve_ip(session, ip, POLICY, pipeline_target=3, now=NOW)

    assert report.planned == 1
    assert again.planned == 0


def test_experiments_that_can_no_longer_produce_evidence_are_concluded(session: Session) -> None:
    dead = make_experiment(session)
    force_video_status(session, dead, VideoStatus.HUMAN_REJECTED)
    weak = evaluated(
        session, 0.3, status=ExperimentStatus.MATURED, video_status=VideoStatus.EVALUATED
    )
    middling = evaluated(
        session, 0.58, status=ExperimentStatus.MATURED, video_status=VideoStatus.EVALUATED
    )

    report = evolve_ip(session, dead.ip, POLICY, pipeline_target=0, now=NOW)

    session.expire_all()
    assert report.conclusions == 3
    assert session.get_one(Experiment, dead.id).conclusion is ExperimentConclusion.INCONCLUSIVE
    assert session.get_one(Experiment, weak.id).conclusion is ExperimentConclusion.NOT_SUPPORTED
    assert session.get_one(Experiment, middling.id).conclusion is ExperimentConclusion.INCONCLUSIVE
    reasons = [
        d.reason
        for d in session.scalars(
            select(SelectionDecision).where(
                SelectionDecision.decision_type == DecisionType.CONCLUDE_EXPERIMENT
            )
        )
    ]
    assert len(reasons) == 3 and all(reasons)


# --- jobs ---------------------------------------------------------------------------------------


def test_evolution_job_runs_the_cycle_for_an_ip(session: Session) -> None:
    strong = evaluated(session, 0.9)

    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = evolution_handlers(POLICY, pipeline_target=2)
    worker = Worker(sessions, handlers, worker_id="w1", clock=lambda: NOW)
    from app.evolution.jobs import enqueue_evolution

    job = enqueue_evolution(session, strong.ip.id, now=NOW)

    worker.run_once()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None and job.result["planned"] == 2
    assert job.result["replication_requests"] == 1
    assert len(jobs(session, EVOLVE_IP)) == 1


def test_recurring_jobs_are_enqueued_once_per_window(session: Session) -> None:
    first = ensure_recurring(session, "evolution_cycle", interval=timedelta(hours=6), now=NOW)
    same = ensure_recurring(
        session, "evolution_cycle", interval=timedelta(hours=6), now=NOW + timedelta(hours=1)
    )
    later = ensure_recurring(
        session, "evolution_cycle", interval=timedelta(hours=6), now=NOW + timedelta(hours=7)
    )

    assert first.id == same.id
    assert later.id != first.id
    assert later.run_at - first.run_at == timedelta(hours=6)
