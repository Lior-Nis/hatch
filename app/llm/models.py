import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.budgets.models import BudgetLedgerEntry
from app.db import Base, Evidence, Identified, JSONDict, Money
from app.experiments.models import Experiment


class ModelCall(Evidence, Identified, Base):
    """One language-model call: exactly what was asked and what came back.
    Together with generation attempts this answers "which prompts, configs and
    models were used?" for any decision."""

    __tablename__ = "model_calls"
    __mutable_columns__ = frozenset({"experiment_id"})

    purpose: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    system: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[JSONDict | None]
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Money | None]
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("budget_ledger.id"))
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    """Set once. Calls that create a candidate happen before it exists and are
    attributed to it afterwards."""

    ledger_entry: Mapped[BudgetLedgerEntry | None] = relationship()
    experiment: Mapped[Experiment | None] = relationship()
