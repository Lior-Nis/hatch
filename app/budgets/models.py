import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ColumnElement, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict, Money, enum_column
from app.experiments.models import Experiment
from app.production.models import GenerationAttempt


class LedgerStatus(StrEnum):
    RESERVED = "reserved"
    """Estimate held before the paid call; counts as committed spend."""
    SETTLED = "settled"
    """The call finished; ``actual_cost_usd`` is set if the provider reported it."""
    BLOCKED = "blocked"
    """Denied by the budget governor. Never counts as spend."""


class BudgetLedgerEntry(Evidence, Identified, Base):
    """One paid external operation — or one blocked attempt at it. Written with
    the estimate before the call; settled once afterwards and never rewritten."""

    __tablename__ = "budget_ledger"
    # experiment_id is set once, when spend made before a candidate existed
    # (e.g. the model call that proposed it) is attributed to it.
    __mutable_columns__ = frozenset({"status", "actual_cost_usd", "settled_at", "experiment_id"})

    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(60))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    generation_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_attempts.id")
    )
    estimated_cost_usd: Mapped[Money]
    actual_cost_usd: Mapped[Money | None]
    status: Mapped[LedgerStatus] = mapped_column(
        enum_column(LedgerStatus), default=LedgerStatus.RESERVED
    )
    block_reason: Mapped[JSONDict | None]
    """For BLOCKED rows: which ceilings the request would have exceeded."""
    settled_at: Mapped[datetime | None]

    experiment: Mapped[Experiment | None] = relationship(back_populates="ledger_entries")
    generation_attempt: Mapped[GenerationAttempt | None] = relationship()


def committed_usd() -> ColumnElement[Decimal]:
    """SQL expression for what a ledger row counts as spend: the actual cost
    when the provider reported one, otherwise the estimate. Combine with
    ``is_spend()`` so blocked attempts are excluded."""
    return func.coalesce(BudgetLedgerEntry.actual_cost_usd, BudgetLedgerEntry.estimated_cost_usd)


def is_spend() -> ColumnElement[bool]:
    return BudgetLedgerEntry.status != LedgerStatus.BLOCKED
