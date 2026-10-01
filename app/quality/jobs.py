"""Queue jobs for automated QA."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from app.quality.ports import QAGate
from app.quality.runner import run_quality_gates
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.worker import JobHandler, JobResult, PermanentJobError
from app.storage import AssetStore

RUN_QA = "run_qa"


def enqueue_qa(
    session: Session, experiment_id: uuid.UUID, *, now: datetime | None = None
) -> JobRun:
    return enqueue(
        session,
        RUN_QA,
        payload={},
        experiment_id=experiment_id,
        idempotency_key=f"{RUN_QA}:{experiment_id}",
        now=now,
    )


def run_qa_handler(*, gates: Sequence[QAGate], store: AssetStore) -> JobHandler:
    def handle(session: Session, job: JobRun) -> JobResult:
        if job.experiment_id is None:
            raise PermanentJobError("run_qa job has no experiment")
        report = run_quality_gates(session, job.experiment_id, gates=gates, store=store)
        return {
            "video_status": report.video_status.value,
            "rejected": report.rejected,
            "escalated": report.escalated,
            "gates": {verdict.gate: verdict.outcome.value for verdict in report.verdicts},
        }

    return handle
