"""Queue jobs for the creative agent."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.budgets.governor import BudgetExceeded, BudgetGovernor
from app.creative.candidates import (
    Planner,
    ProductionDefaults,
    propose_mutation,
    propose_novel,
)
from app.evolution.anti_cloning import AntiCloningPolicy
from app.experiments.models import Experiment
from app.experiments.spec import OutputRequirements
from app.ips.models import IP
from app.llm.ports import LanguageModel, LLMError
from app.production.jobs import enqueue_production
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError

PROPOSE_NOVEL = "propose_novel"
PROPOSE_MUTATION = "propose_mutation"


def enqueue_novel_proposal(
    session: Session,
    ip_id: uuid.UUID,
    *,
    produce: bool = False,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> JobRun:
    return enqueue(
        session,
        PROPOSE_NOVEL,
        payload={"ip_id": str(ip_id), "produce": produce},
        idempotency_key=idempotency_key,
        now=now,
    )


def enqueue_mutation_proposal(
    session: Session,
    parent_experiment_id: uuid.UUID,
    *,
    produce: bool = False,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> JobRun:
    return enqueue(
        session,
        PROPOSE_MUTATION,
        payload={"parent_experiment_id": str(parent_experiment_id), "produce": produce},
        idempotency_key=idempotency_key,
        now=now,
    )


def _finish(session: Session, job: JobRun, candidate: Experiment) -> JobResult:
    job.experiment_id = candidate.id
    if job.payload.get("produce"):
        enqueue_production(session, candidate.id, now=job.started_at)
    return {"experiment_id": str(candidate.id)}


def creative_handlers(
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    output: OutputRequirements,
    production: ProductionDefaults | Planner,
    policy: AntiCloningPolicy | None = None,
) -> dict[str, JobHandler]:
    def guarded(propose: JobHandler) -> JobHandler:
        def handle(session: Session, job: JobRun) -> JobResult:
            if job.result and job.result.get("experiment_id"):
                return job.result  # replayed job: the candidate already exists
            try:
                return propose(session, job)
            except BudgetExceeded as exc:
                raise PermanentJobError(str(exc)) from exc
            except LLMError as exc:
                if exc.retryable:
                    raise
                raise PermanentJobError(str(exc)) from exc

        return handle

    def novel(session: Session, job: JobRun) -> JobResult:
        ip = session.get_one(IP, uuid.UUID(job.payload["ip_id"]))
        candidate = propose_novel(
            session,
            ip,
            llm=llm,
            governor=governor,
            output=output,
            production=production,
            policy=policy,
        )
        return _finish(session, job, candidate)

    def mutation(session: Session, job: JobRun) -> JobResult:
        parent = session.get_one(Experiment, uuid.UUID(job.payload["parent_experiment_id"]))
        candidate = propose_mutation(session, parent, llm=llm, governor=governor, policy=policy)
        return _finish(session, job, candidate)

    return {PROPOSE_NOVEL: guarded(novel), PROPOSE_MUTATION: guarded(mutation)}
