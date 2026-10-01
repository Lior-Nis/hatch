"""Budget governor: the only gate in front of paid external calls.

Every paid operation must ``reserve`` its estimated cost first. A reservation
that would push committed spend past any hard ceiling is refused, and the
refusal is itself written to the ledger. Ceilings come from operator-controlled
settings; nothing in the application can raise them.

Committed spend = actual cost when the provider reported one, otherwise the
estimate. Daily and monthly ceilings use rolling 24-hour and 30-day windows,
which cannot be gamed by bursting across a calendar boundary.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.budgets.models import BudgetLedgerEntry, LedgerStatus, committed_usd, is_spend
from app.config import Settings
from app.db import utcnow

logger = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 0x4841544348  # "HATCH": serialises concurrent reservations


class BudgetLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_per_generation_usd: Decimal
    max_per_video_usd: Decimal
    daily_usd: Decimal
    monthly_usd: Decimal

    @classmethod
    def from_settings(cls, settings: Settings) -> "BudgetLimits":
        return cls(
            max_per_generation_usd=settings.budget_max_per_generation_usd,
            max_per_video_usd=settings.budget_max_per_video_usd,
            daily_usd=settings.budget_daily_usd,
            monthly_usd=settings.budget_monthly_usd,
        )


class LimitViolation(BaseModel):
    model_config = ConfigDict(frozen=True)

    limit: str
    ceiling_usd: Decimal
    committed_usd: Decimal
    requested_usd: Decimal


class BudgetExceeded(Exception):
    def __init__(self, violations: list[LimitViolation], ledger_entry_id: uuid.UUID) -> None:
        self.violations = violations
        self.ledger_entry_id = ledger_entry_id
        summary = "; ".join(
            f"{v.limit}: ${v.committed_usd} committed + ${v.requested_usd} requested "
            f"> ${v.ceiling_usd} ceiling"
            for v in violations
        )
        super().__init__(f"budget exceeded ({summary})")


class BudgetGovernor:
    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._limits = limits
        self._clock = clock

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def reserve(
        self,
        session: Session,
        *,
        provider: str,
        model: str,
        operation: str,
        estimated_cost_usd: Decimal,
        experiment_id: uuid.UUID,
        generation_attempt_id: uuid.UUID | None = None,
    ) -> BudgetLedgerEntry:
        """Hold ``estimated_cost_usd`` against every ceiling, or raise
        ``BudgetExceeded`` after logging the refusal in the ledger."""
        # Serialise reservations so two workers cannot both fit under a ceiling.
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
        now = self._clock()
        violations = self._violations(session, experiment_id, estimated_cost_usd, now)
        entry = BudgetLedgerEntry(
            provider=provider,
            model=model,
            operation=operation,
            experiment_id=experiment_id,
            generation_attempt_id=generation_attempt_id,
            estimated_cost_usd=estimated_cost_usd,
            status=LedgerStatus.BLOCKED if violations else LedgerStatus.RESERVED,
            block_reason=(
                {"violations": [v.model_dump(mode="json") for v in violations]}
                if violations
                else None
            ),
            created_at=now,
        )
        session.add(entry)
        session.flush()
        if violations:
            logger.warning(
                "budget_blocked",
                extra={
                    "experiment_id": str(experiment_id),
                    "estimated_cost_usd": str(estimated_cost_usd),
                    "limits": [v.limit for v in violations],
                },
            )
            raise BudgetExceeded(violations, entry.id)
        return entry

    def settle(
        self, session: Session, entry: BudgetLedgerEntry, *, actual_cost_usd: Decimal | None
    ) -> None:
        """Close a reservation once the paid call has finished. Pass the
        provider-reported cost when known; ``None`` keeps counting the estimate."""
        if entry.status is not LedgerStatus.RESERVED:
            raise ValueError(f"ledger entry {entry.id} is already settled or was blocked")
        entry.actual_cost_usd = actual_cost_usd
        entry.status = LedgerStatus.SETTLED
        entry.settled_at = self._clock()
        session.flush()

    def committed_spend_for_experiment(self, session: Session, experiment_id: uuid.UUID) -> Decimal:
        return self._committed(session, BudgetLedgerEntry.experiment_id == experiment_id)

    def committed_spend_since(self, session: Session, since: datetime) -> Decimal:
        return self._committed(session, BudgetLedgerEntry.created_at > since)

    def _committed(self, session: Session, condition: ColumnElement[bool]) -> Decimal:
        total = session.scalar(
            select(func.coalesce(func.sum(committed_usd()), 0)).where(is_spend(), condition)
        )
        return Decimal(total or 0)

    def _violations(
        self, session: Session, experiment_id: uuid.UUID, requested: Decimal, now: datetime
    ) -> list[LimitViolation]:
        checks = [
            ("per_generation", self._limits.max_per_generation_usd, Decimal("0")),
            (
                "per_video",
                self._limits.max_per_video_usd,
                self.committed_spend_for_experiment(session, experiment_id),
            ),
            (
                "daily",
                self._limits.daily_usd,
                self.committed_spend_since(session, now - timedelta(hours=24)),
            ),
            (
                "monthly",
                self._limits.monthly_usd,
                self.committed_spend_since(session, now - timedelta(days=30)),
            ),
        ]
        return [
            LimitViolation(
                limit=name, ceiling_usd=ceiling, committed_usd=committed, requested_usd=requested
            )
            for name, ceiling, committed in checks
            if committed + requested > ceiling
        ]
