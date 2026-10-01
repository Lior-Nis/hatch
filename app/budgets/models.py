import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, Money
from app.experiments.models import Experiment
from app.production.models import GenerationAttempt


class BudgetLedgerEntry(Identified, Base):
    """One paid external operation. Written with the estimate before the call;
    the actual cost is settled once afterwards and never rewritten."""

    __tablename__ = "budget_ledger"

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
    settled_at: Mapped[datetime | None]

    experiment: Mapped[Experiment | None] = relationship(back_populates="ledger_entries")
    generation_attempt: Mapped[GenerationAttempt | None] = relationship()
