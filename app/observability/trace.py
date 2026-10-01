"""Experiment trace: every recorded event for one experiment, in order.

Lets an operator follow an experiment — especially a failed one — from creation
through jobs, budget decisions, generation attempts, QA, review, and
publication without reading database rows.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.budgets.models import LedgerStatus
from app.db import utcnow
from app.experiments.lineage import ExperimentNotFound
from app.experiments.models import Experiment
from app.production.models import AttemptStatus, ProductionStep, StepStatus
from app.quality.ports import QAOutcome
from app.scheduling.models import JobStatus

Level = Literal["info", "warning", "error"]


class TimelineEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: datetime
    kind: str
    level: Level = "info"
    detail: str


def experiment_timeline(session: Session, experiment_id: uuid.UUID) -> list[TimelineEvent]:
    experiment = session.scalars(
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .options(
            selectinload(Experiment.generation_attempts),
            selectinload(Experiment.ledger_entries),
            selectinload(Experiment.assets),
            selectinload(Experiment.qa_results),
            selectinload(Experiment.human_reviews),
            selectinload(Experiment.publications),
            selectinload(Experiment.job_runs),
        )
    ).one_or_none()
    if experiment is None:
        raise ExperimentNotFound(str(experiment_id))

    events: list[TimelineEvent] = []

    def add(at: datetime | None, kind: str, detail: str, level: Level = "info") -> None:
        if at is not None:
            events.append(TimelineEvent(at=at, kind=kind, level=level, detail=detail))

    add(
        experiment.created_at,
        "experiment_created",
        f"IP {experiment.ip.slug}; hypothesis: {experiment.hypothesis.statement} "
        f"Reason: {experiment.generation_reason}",
    )

    for job in experiment.job_runs:
        label = f"job {job.job_type} ({job.id})"
        add(job.created_at, "job_queued", label)
        if job.status is JobStatus.SUCCEEDED:
            add(job.finished_at, "job_succeeded", f"{label} after {job.attempts} attempt(s)")
        elif job.status is JobStatus.FAILED:
            add(
                job.finished_at,
                "job_failed",
                f"{label} gave up after {job.attempts}/{job.max_attempts} attempt(s): {job.error}",
                "error",
            )
        elif job.status is JobStatus.RUNNING:
            add(job.started_at, "job_running", f"{label} held by {job.locked_by}")
        elif job.error:
            add(
                job.started_at,
                "job_retrying",
                f"{label} failed attempt {job.attempts}/{job.max_attempts}: {job.error}; "
                f"next run at {job.run_at.isoformat()}",
                "warning",
            )

    for entry in experiment.ledger_entries:
        what = f"{entry.provider} {entry.model} {entry.operation}"
        if entry.status is LedgerStatus.BLOCKED:
            violations = (entry.block_reason or {}).get("violations", [])
            limits = ", ".join(
                f"{v['limit']} (${v['committed_usd']} + ${v['requested_usd']} "
                f"> ${v['ceiling_usd']})"
                for v in violations
            )
            add(
                entry.created_at,
                "budget_blocked",
                f"${entry.estimated_cost_usd:.2f} for {what} refused: {limits}",
                "error",
            )
            continue
        add(entry.created_at, "budget_reserved", f"${entry.estimated_cost_usd:.2f} for {what}")
        if entry.status is LedgerStatus.SETTLED:
            actual = (
                f"${entry.actual_cost_usd:.2f} actual"
                if entry.actual_cost_usd is not None
                else "provider reported no cost; estimate stands"
            )
            add(entry.settled_at, "budget_settled", f"{what}: {actual}")

    for attempt in experiment.generation_attempts:
        label = (
            f"attempt {attempt.attempt_number} ({attempt.strategy}) on "
            f"{attempt.provider} {attempt.model}"
        )
        if attempt.status is AttemptStatus.BLOCKED_BUDGET:
            add(attempt.finished_at, "generation_blocked", f"{label}: {attempt.error}", "error")
            continue
        add(
            attempt.started_at,
            "generation_started",
            f"{label}, provider job {attempt.provider_job_id}",
        )
        if attempt.status is AttemptStatus.SUCCEEDED:
            add(attempt.finished_at, "generation_succeeded", label)
        elif attempt.status is AttemptStatus.FAILED:
            add(attempt.finished_at, "generation_failed", f"{label}: {attempt.error}", "error")

    for step in session.scalars(
        select(ProductionStep).where(ProductionStep.experiment_id == experiment.id)
    ):
        if step.status is StepStatus.FAILED:
            add(step.finished_at, "step_failed", f"{step.key}: {step.error}", "error")
        elif step.status is StepStatus.SUCCEEDED:
            add(step.finished_at, "step_succeeded", step.key)

    for asset in experiment.assets:
        info = asset.media_info
        add(
            asset.created_at,
            "asset_stored",
            f"{asset.kind.value} {info.get('width')}x{info.get('height')} "
            f"{info.get('duration_seconds')}s, {asset.size_bytes} bytes at {asset.storage_uri}",
        )

    for result in experiment.qa_results:
        failed = result.outcome is QAOutcome.FAIL
        level: Level = "info"
        if failed and result.mandatory:
            level = "error"
        elif result.outcome is not QAOutcome.PASS:
            level = "warning"
        reasons = "; ".join(result.reasons) or "no issues"
        gate = f"{result.gate} v{result.gate_version}" + (
            " (mandatory)" if result.mandatory else ""
        )
        add(result.created_at, f"qa_{result.outcome.value}", f"{gate}: {reasons}", level)

    for review in experiment.human_reviews:
        add(
            review.created_at,
            f"human_{review.decision.value}",
            f"{review.reviewer}: {review.reason or 'no reason given'}",
        )

    for publication in experiment.publications:
        at = publication.published_at or publication.scheduled_at or publication.created_at
        add(
            at,
            f"publication_{publication.status.value}",
            f"{publication.platform.value} post {publication.platform_post_id or '-'}",
            "error" if publication.status.value == "failed" else "info",
        )

    events.sort(key=lambda event: event.at)
    conclusion = f", conclusion {experiment.conclusion.value}" if experiment.conclusion else ""
    events.append(
        TimelineEvent(
            at=max(utcnow(), events[-1].at),
            kind="current_state",
            detail=(
                f"experiment {experiment.status.value}{conclusion}; video "
                f"{experiment.video_status.value} since "
                f"{experiment.video_status_changed_at.isoformat(timespec='seconds')}"
            ),
        )
    )
    return events


def render_timeline(events: list[TimelineEvent]) -> str:
    marks = {"info": " ", "warning": "!", "error": "✗"}
    return "\n".join(
        f"{event.at.strftime('%Y-%m-%d %H:%M:%S')} {marks[event.level]} "
        f"{event.kind:<22} {event.detail}"
        for event in events
    )
