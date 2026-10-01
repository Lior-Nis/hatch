"""Queue jobs for fitness evaluation."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.analytics.models import MetricSnapshot
from app.fitness.heuristic import InsufficientSignals
from app.fitness.ports import FitnessEvaluator
from app.fitness.service import advance_for_checkpoint, evaluate_snapshot
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult

EVALUATE_FITNESS = "evaluate_fitness"


def enqueue_fitness_evaluation(
    session: Session, snapshot: MetricSnapshot, *, now: datetime | None = None
) -> JobRun:
    return enqueue(
        session,
        EVALUATE_FITNESS,
        payload={"snapshot_id": str(snapshot.id), "checkpoint": snapshot.checkpoint},
        experiment_id=snapshot.publication.experiment_id,
        idempotency_key=f"{EVALUATE_FITNESS}:{snapshot.id}",
        now=now,
    )


def fitness_handlers(
    evaluator: FitnessEvaluator, *, target_cost_usd: Decimal | None
) -> dict[str, JobHandler]:
    def evaluate(session: Session, job: JobRun) -> JobResult:
        snapshot = session.get_one(MetricSnapshot, job.payload["snapshot_id"])
        experiment = snapshot.publication.experiment
        try:
            fitness = evaluate_snapshot(
                session, snapshot.id, evaluator, target_cost_usd=target_cost_usd, strict=True
            )
        except InsufficientSignals as exc:
            advance_for_checkpoint(experiment, snapshot.checkpoint)
            return {"scored": False, "reason": str(exc)}
        assert fitness is not None
        advance_for_checkpoint(experiment, snapshot.checkpoint)
        return {"scored": True, "fitness_snapshot_id": str(fitness.id), "score": fitness.score}

    return {EVALUATE_FITNESS: evaluate}
