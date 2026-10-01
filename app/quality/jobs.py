"""Queue jobs for automated QA."""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.quality.ports import QAGate
from app.quality.runner import run_quality_gates
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError
from app.storage import AssetStore

RUN_QA = "run_qa"
# The production job type, named here rather than imported: production.jobs
# imports this module to queue QA after generation.
_PRODUCE = "produce_short"


def enqueue_qa(
    session: Session,
    experiment_id: uuid.UUID,
    asset_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> JobRun:
    """One QA job per finished video (a regenerated video is a new asset and
    gets its own QA run)."""
    return enqueue(
        session,
        RUN_QA,
        payload={"asset_id": str(asset_id)},
        experiment_id=experiment_id,
        idempotency_key=f"{RUN_QA}:{asset_id}",
        now=now,
    )


GateFactory = Callable[[Session], Sequence[QAGate]]
"""Builds the gates for one QA run; model-based gates need the run's session
to record and budget their calls."""


def run_qa_handler(
    *, gates: Sequence[QAGate] | GateFactory, store: AssetStore, max_regenerations: int = 1
) -> JobHandler:
    def handle(session: Session, job: JobRun) -> JobResult:
        if job.experiment_id is None:
            raise PermanentJobError("run_qa job has no experiment")
        run_gates = gates(session) if callable(gates) else gates
        report = run_quality_gates(session, job.experiment_id, gates=run_gates, store=store)
        regenerating = False
        if report.rejected:
            # Generate → QA → if fail: retry. The rejected asset stays rejected;
            # production makes a new one, which is checked from scratch.
            done = (
                session.scalar(
                    select(func.count())
                    .select_from(JobRun)
                    .where(
                        JobRun.experiment_id == job.experiment_id,
                        JobRun.job_type == _PRODUCE,
                        JobRun.idempotency_key.like("%:regeneration:%"),
                    )
                )
                or 0
            )
            if done < max_regenerations:
                enqueue(
                    session,
                    _PRODUCE,
                    payload={"regeneration": done + 1},
                    experiment_id=job.experiment_id,
                    idempotency_key=f"{_PRODUCE}:{job.experiment_id}:regeneration:{done + 1}",
                    now=job.started_at,
                )
                regenerating = True
        return {
            "video_status": report.video_status.value,
            "rejected": report.rejected,
            "escalated": report.escalated,
            "regenerating": regenerating,
            "gates": {verdict.gate: verdict.outcome.value for verdict in report.verdicts},
        }

    return handle
