from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.jobs import (
    PROPOSE_MUTATION,
    PROPOSE_NOVEL,
    creative_handlers,
    enqueue_mutation_proposal,
    enqueue_novel_proposal,
)
from app.evolution.models import ExperimentParent
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.llm.ports import LLMError
from app.production.jobs import PRODUCE_SHORT
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.worker import Worker
from integrations.fake.llm import FakeLanguageModel
from tests.creative.test_hypotheses import DEFAULTS, draft, novel_draft
from tests.factories import LIMITS, make_experiment


def worker(session: Session, llm: FakeLanguageModel) -> Worker:
    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = creative_handlers(
        llm=llm, governor=BudgetGovernor(LIMITS), output=FIRST_SHORT.output, production=DEFAULTS
    )
    return Worker(sessions, handlers, worker_id="w1")


def test_novelty_job_produces_a_valid_candidate_without_selecting_a_parent(
    session: Session,
) -> None:
    ip = make_experiment(session).ip
    job = enqueue_novel_proposal(session, ip.id)

    worker(session, FakeLanguageModel([novel_draft()])).run_once()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    candidate = session.get_one(Experiment, job.result["experiment_id"])
    assert candidate.ip_id == ip.id
    assert session.scalars(select(ExperimentParent)).all() == []
    assert candidate.genome.genes["hook_type"] == "sound_first"
    # The job now belongs to the experiment it created.
    assert job.experiment_id == candidate.id


def test_mutation_job_creates_a_descendant_and_queues_its_production(session: Session) -> None:
    parent = make_experiment(session)
    job = enqueue_mutation_proposal(session, parent.id, produce=True)

    worker(session, FakeLanguageModel([draft()], cost_usd=Decimal("0.02"))).run_once()

    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    child = session.get_one(Experiment, job.result["experiment_id"])
    assert [link.parent_id for link in child.parents] == [parent.id]
    queued = session.scalars(select(JobRun).where(JobRun.job_type == PRODUCE_SHORT)).one()
    assert queued.experiment_id == child.id


def test_candidates_are_not_produced_unless_asked(session: Session) -> None:
    ip = make_experiment(session).ip
    enqueue_novel_proposal(session, ip.id)

    worker(session, FakeLanguageModel([novel_draft()])).run_once()

    assert session.scalars(select(JobRun).where(JobRun.job_type == PRODUCE_SHORT)).all() == []


def test_a_transient_model_failure_is_retried_and_a_permanent_one_is_not(session: Session) -> None:
    ip = make_experiment(session).ip
    flaky = enqueue_novel_proposal(session, ip.id)
    worker(session, FakeLanguageModel([LLMError("overloaded", retryable=True)])).run_once()
    assert flaky.status is JobStatus.QUEUED

    session.delete(flaky)
    session.flush()
    doomed = enqueue_novel_proposal(session, ip.id)
    worker(session, FakeLanguageModel([LLMError("bad request", retryable=False)])).run_once()
    assert doomed.status is JobStatus.FAILED


def test_job_types_are_registered() -> None:
    handlers = creative_handlers(
        llm=FakeLanguageModel([]),
        governor=BudgetGovernor(LIMITS),
        output=FIRST_SHORT.output,
        production=DEFAULTS,
    )
    assert set(handlers) == {PROPOSE_NOVEL, PROPOSE_MUTATION}
