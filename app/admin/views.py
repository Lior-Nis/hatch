"""Read models for the operator dashboard. Queries only: nothing here writes.

They answer the operator's questions: what is running, what was generated and
published, what did it cost, what is winning or dying, why did the system
decide that, and what is waiting for approval.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetLimits
from app.budgets.models import BudgetLedgerEntry, committed_usd, is_spend
from app.budgets.reports import SpendReport, spend_report
from app.db import utcnow
from app.evolution.evidence import experiment_evidence
from app.evolution.models import ExperimentParent, SelectionDecision
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion, VideoStatus
from app.fitness.ip_aggregate import latest_ip_fitness
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.ips.models import IP
from app.knowledge.models import KnowledgeSummary
from app.observability.health import ProviderHealth, Stall, find_stalls, provider_health
from app.scheduling.models import JobRun, JobStatus

_IN_PIPELINE = (
    VideoStatus.PROPOSED,
    VideoStatus.SCRIPTED,
    VideoStatus.STORYBOARDED,
    VideoStatus.GENERATING,
    VideoStatus.GENERATED,
    VideoStatus.QA_PENDING,
    VideoStatus.READY,
    VideoStatus.SCHEDULED,
)
_LIVE = (VideoStatus.PUBLISHED, VideoStatus.OBSERVING, VideoStatus.EVALUATED)


def _spend_since(session: Session, since: datetime, ip_id: uuid.UUID | None = None) -> Decimal:
    query = select(func.coalesce(func.sum(committed_usd()), 0)).where(
        is_spend(), BudgetLedgerEntry.created_at > since
    )
    if ip_id is not None:
        query = query.join(Experiment, Experiment.id == BudgetLedgerEntry.experiment_id).where(
            Experiment.ip_id == ip_id
        )
    return Decimal(session.scalar(query) or 0)


def budget_status(session: Session, limits: BudgetLimits, now: datetime) -> dict[str, Decimal]:
    spent_day = _spend_since(session, now - timedelta(hours=24))
    spent_month = _spend_since(session, now - timedelta(days=30))
    return {
        "spent_24h": spent_day,
        "daily_limit": limits.daily_usd,
        "remaining_today": max(Decimal("0"), limits.daily_usd - spent_day),
        "spent_30d": spent_month,
        "monthly_limit": limits.monthly_usd,
        "remaining_30d": max(Decimal("0"), limits.monthly_usd - spent_month),
        "max_per_video": limits.max_per_video_usd,
    }


def job_counts(session: Session) -> dict[str, int]:
    counts = dict(
        session.execute(select(JobRun.status, func.count()).group_by(JobRun.status)).all()
    )
    return {status.value: counts.get(status, 0) for status in JobStatus}


def portfolio(
    session: Session, limits: BudgetLimits, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    rows = []
    for ip in session.scalars(select(IP).order_by(IP.slug)):
        statuses = session.scalars(
            select(Experiment.video_status).where(Experiment.ip_id == ip.id)
        ).all()
        conclusions = session.scalars(
            select(Experiment.conclusion).where(Experiment.ip_id == ip.id)
        ).all()
        fitness = latest_ip_fitness(session, ip)
        last = session.scalars(
            select(SelectionDecision)
            .where(SelectionDecision.ip_id == ip.id)
            .order_by(SelectionDecision.created_at.desc())
        ).first()
        rows.append(
            {
                "ip": ip,
                "score": fitness.score if fitness else None,
                "sufficient": bool(fitness and fitness.inputs.get("sufficient_evidence")),
                "experiments": len(statuses),
                "in_pipeline": sum(s in _IN_PIPELINE for s in statuses),
                "awaiting_review": sum(s is VideoStatus.APPROVAL_PENDING for s in statuses),
                "live": sum(s in _LIVE for s in statuses),
                "supported": sum(c is ExperimentConclusion.SUPPORTED for c in conclusions),
                "spend_30d": _spend_since(session, now - timedelta(days=30), ip.id),
                "last_decision": last,
            }
        )
    return {
        "ips": rows,
        "budget": budget_status(session, limits, now),
        "jobs": job_counts(session),
        "stalls": find_stalls(session, now=now),
        "awaiting_review": sum(row["awaiting_review"] for row in rows),
    }


def _experiment_rows(session: Session, experiments: list[Experiment]) -> list[dict[str, Any]]:
    relation = {
        link.experiment_id: link
        for link in session.scalars(
            select(ExperimentParent).where(
                ExperimentParent.experiment_id.in_([e.id for e in experiments])
            )
        )
    }
    rows = []
    for experiment in experiments:
        evidence = experiment_evidence(session, experiment)
        link = relation.get(experiment.id)
        rows.append(
            {
                "experiment": experiment,
                "score": evidence.score,
                "platforms": evidence.platforms,
                "relation": link.relation.value if link else "novel",
                "parent_id": link.parent_id if link else None,
            }
        )
    return rows


def ip_detail(session: Session, slug: str) -> dict[str, Any] | None:
    ip = session.scalars(select(IP).where(IP.slug == slug)).one_or_none()
    if ip is None:
        return None
    experiments = list(
        session.scalars(
            select(Experiment)
            .where(Experiment.ip_id == ip.id)
            .order_by(Experiment.created_at.desc())
            .limit(200)
        )
    )
    return {
        "ip": ip,
        "fitness": latest_ip_fitness(session, ip),
        "experiments": _experiment_rows(session, experiments),
        "decisions": session.scalars(
            select(SelectionDecision)
            .where(SelectionDecision.ip_id == ip.id)
            .order_by(SelectionDecision.created_at.desc())
            .limit(30)
        ).all(),
        "knowledge": session.scalars(
            select(KnowledgeSummary)
            .where(KnowledgeSummary.ip_id == ip.id)
            .order_by(KnowledgeSummary.confidence.desc())
        ).all(),
    }


def experiment_relations(session: Session, experiment_id: uuid.UUID) -> dict[str, Any]:
    """Everything around one experiment that its own lineage record does not
    hold: its children, the decisions about it, and its fitness history."""
    children = session.scalars(
        select(ExperimentParent).where(ExperimentParent.parent_id == experiment_id)
    ).all()
    parents = session.scalars(
        select(ExperimentParent).where(ExperimentParent.experiment_id == experiment_id)
    ).all()
    decisions = session.scalars(
        select(SelectionDecision)
        .where(
            (SelectionDecision.subject_experiment_id == experiment_id)
            | (SelectionDecision.resulting_experiment_id == experiment_id)
        )
        .order_by(SelectionDecision.created_at)
    ).all()
    fitness = session.scalars(
        select(FitnessSnapshot)
        .where(
            FitnessSnapshot.scope == FitnessScope.VIDEO,
            FitnessSnapshot.experiment_id == experiment_id,
        )
        .order_by(FitnessSnapshot.created_at)
    ).all()
    return {"children": children, "parents": parents, "decisions": decisions, "fitness": fitness}


def queue(session: Session, now: datetime | None = None) -> dict[str, Any]:
    def jobs(*statuses: JobStatus, limit: int) -> list[JobRun]:
        return list(
            session.scalars(
                select(JobRun)
                .where(JobRun.status.in_(statuses))
                .order_by(JobRun.created_at.desc())
                .limit(limit)
            )
        )

    return {
        "counts": job_counts(session),
        "failed": jobs(JobStatus.FAILED, limit=50),
        "active": jobs(JobStatus.RUNNING, JobStatus.QUEUED, limit=100),
        "recent": jobs(JobStatus.SUCCEEDED, limit=30),
        "stalls": find_stalls(session, now=now),
    }


def costs(session: Session, limits: BudgetLimits, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    report: SpendReport = spend_report(session, since=now - timedelta(days=30))
    health: list[ProviderHealth] = provider_health(session, since=now - timedelta(days=30))
    return {"report": report, "budget": budget_status(session, limits, now), "providers": health}


def decisions(
    session: Session, *, ip_slug: str | None = None, limit: int = 100
) -> list[SelectionDecision]:
    query = select(SelectionDecision).order_by(SelectionDecision.created_at.desc()).limit(limit)
    if ip_slug:
        query = query.join(IP, IP.id == SelectionDecision.ip_id).where(IP.slug == ip_slug)
    return list(session.scalars(query))


__all__ = [
    "Stall",
    "budget_status",
    "costs",
    "decisions",
    "experiment_relations",
    "ip_detail",
    "portfolio",
    "queue",
]
