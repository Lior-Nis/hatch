"""Cost reporting over the budget ledger.

Spend is always *committed* spend (actual when reported, else the estimate) and
never includes blocked attempts. Ratios divide total spend by a count of
videos, so the cost of failed and rejected attempts is carried by the videos
that made it through — which is what a video really costs.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, Date, cast, func, select
from sqlalchemy.orm import Session

from app.budgets.models import BudgetLedgerEntry, LedgerStatus, committed_usd, is_spend
from app.experiments.models import Experiment
from app.experiments.states import ACCEPTED_VIDEO_STATES, PUBLISHED_VIDEO_STATES
from app.ips.models import IP


class SpendReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_usd: Decimal
    by_experiment: dict[uuid.UUID, Decimal]
    by_ip: dict[str, Decimal]
    by_provider: dict[str, Decimal]
    by_day: dict[date, Decimal]
    """UTC calendar days."""
    videos: int
    """Experiments with any spend in the window."""
    accepted_videos: int
    published_videos: int
    blocked_attempts: int
    cost_per_video_usd: Decimal | None
    cost_per_accepted_video_usd: Decimal | None
    cost_per_published_video_usd: Decimal | None


def _per(total: Decimal, count: int) -> Decimal | None:
    return (total / count).quantize(Decimal("0.01")) if count else None


def spend_report(
    session: Session, *, since: datetime | None = None, until: datetime | None = None
) -> SpendReport:
    window: list[ColumnElement[bool]] = []
    if since is not None:
        window.append(BudgetLedgerEntry.created_at >= since)
    if until is not None:
        window.append(BudgetLedgerEntry.created_at < until)
    spend = [is_spend(), *window]
    amount = func.sum(committed_usd())

    total = Decimal(session.scalar(select(func.coalesce(amount, 0)).where(*spend)) or 0)
    by_experiment = {
        experiment_id: Decimal(value)
        for experiment_id, value in session.execute(
            select(BudgetLedgerEntry.experiment_id, amount)
            .where(*spend, BudgetLedgerEntry.experiment_id.is_not(None))
            .group_by(BudgetLedgerEntry.experiment_id)
        )
    }
    by_ip = {
        slug: Decimal(value)
        for slug, value in session.execute(
            select(IP.slug, amount)
            .join(Experiment, Experiment.id == BudgetLedgerEntry.experiment_id)
            .join(IP, IP.id == Experiment.ip_id)
            .where(*spend)
            .group_by(IP.slug)
        )
    }
    by_provider = {
        provider: Decimal(value)
        for provider, value in session.execute(
            select(BudgetLedgerEntry.provider, amount)
            .where(*spend)
            .group_by(BudgetLedgerEntry.provider)
        )
    }
    day = cast(func.timezone("UTC", BudgetLedgerEntry.created_at), Date)
    by_day = {
        when: Decimal(value)
        for when, value in session.execute(
            select(day, amount).where(*spend).group_by(day).order_by(day)
        )
    }
    blocked = session.scalar(
        select(func.count())
        .select_from(BudgetLedgerEntry)
        .where(BudgetLedgerEntry.status == LedgerStatus.BLOCKED, *window)
    )

    statuses = session.scalars(
        select(Experiment.video_status).where(Experiment.id.in_(by_experiment.keys()))
    ).all()
    accepted = sum(1 for status in statuses if status in ACCEPTED_VIDEO_STATES)
    published = sum(1 for status in statuses if status in PUBLISHED_VIDEO_STATES)
    return SpendReport(
        total_usd=total,
        by_experiment=by_experiment,
        by_ip=by_ip,
        by_provider=by_provider,
        by_day=by_day,
        videos=len(by_experiment),
        accepted_videos=accepted,
        published_videos=published,
        blocked_attempts=blocked or 0,
        cost_per_video_usd=_per(total, len(by_experiment)),
        cost_per_accepted_video_usd=_per(total, accepted),
        cost_per_published_video_usd=_per(total, published),
    )
