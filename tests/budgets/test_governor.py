from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetExceeded, BudgetGovernor, BudgetLimits
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.config import Settings
from app.experiments.models import Experiment
from tests.factories import make_experiment

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LIMITS = BudgetLimits(
    max_per_generation_usd=Decimal("0.75"),
    max_per_video_usd=Decimal("1.50"),
    daily_usd=Decimal("15.00"),
    monthly_usd=Decimal("500.00"),
)


def governor(now: datetime = NOW) -> BudgetGovernor:
    return BudgetGovernor(LIMITS, clock=lambda: now)


def reserve(
    session: Session, experiment: Experiment, amount: str, *, now: datetime = NOW
) -> BudgetLedgerEntry:
    return governor(now).reserve(
        session,
        provider="fake",
        model="fake-video-1",
        operation="text_to_video",
        estimated_cost_usd=Decimal(amount),
        experiment_id=experiment.id,
    )


def spend(session: Session, experiment: Experiment, amount: str, *, at: datetime) -> None:
    """Record already-settled spend at a point in time."""
    session.add(
        BudgetLedgerEntry(
            provider="fake",
            model="fake-video-1",
            operation="text_to_video",
            experiment_id=experiment.id,
            estimated_cost_usd=Decimal(amount),
            actual_cost_usd=Decimal(amount),
            status=LedgerStatus.SETTLED,
            created_at=at,
            settled_at=at,
        )
    )
    session.flush()


def test_limits_come_from_settings() -> None:
    limits = BudgetLimits.from_settings(Settings(_env_file=None))

    assert limits.max_per_video_usd == Decimal("1.50")
    assert limits.daily_usd == Decimal("15.00")
    assert limits.monthly_usd == Decimal("500.00")
    assert limits.max_per_generation_usd <= limits.max_per_video_usd


def test_reservation_within_limits_records_the_estimate(session: Session) -> None:
    experiment = make_experiment(session)

    entry = reserve(session, experiment, "0.40")

    assert entry.status is LedgerStatus.RESERVED
    assert entry.estimated_cost_usd == Decimal("0.40")
    assert entry.actual_cost_usd is None
    assert entry.created_at == NOW


def test_generation_above_the_per_generation_ceiling_is_blocked(session: Session) -> None:
    experiment = make_experiment(session)

    with pytest.raises(BudgetExceeded) as blocked:
        reserve(session, experiment, "0.80")

    assert [v.limit for v in blocked.value.violations] == ["per_generation"]


def test_a_blocked_attempt_is_logged_in_the_ledger_and_costs_nothing(session: Session) -> None:
    experiment = make_experiment(session)

    with pytest.raises(BudgetExceeded):
        reserve(session, experiment, "0.80")

    [entry] = session.scalars(select(BudgetLedgerEntry)).all()
    assert entry.status is LedgerStatus.BLOCKED
    assert entry.estimated_cost_usd == Decimal("0.80")
    assert entry.block_reason is not None
    assert entry.block_reason["violations"][0]["limit"] == "per_generation"
    assert governor().committed_spend_for_experiment(session, experiment.id) == Decimal("0")


def test_spend_exactly_reaching_a_ceiling_is_allowed(session: Session) -> None:
    experiment = make_experiment(session)
    reserve(session, experiment, "0.75")

    entry = reserve(session, experiment, "0.75")

    assert entry.status is LedgerStatus.RESERVED


def test_per_video_ceiling_blocks_when_prior_attempts_used_the_budget(session: Session) -> None:
    experiment = make_experiment(session)
    other = make_experiment(session)
    reserve(session, experiment, "0.60")
    reserve(session, experiment, "0.60")

    with pytest.raises(BudgetExceeded) as blocked:
        reserve(session, experiment, "0.40")

    assert [v.limit for v in blocked.value.violations] == ["per_video"]
    assert reserve(session, other, "0.40").status is LedgerStatus.RESERVED


def test_daily_ceiling_counts_only_the_last_24_hours(session: Session) -> None:
    old, recent, current = (make_experiment(session) for _ in range(3))
    spend(session, old, "100.00", at=NOW - timedelta(hours=25))
    spend(session, recent, "14.80", at=NOW - timedelta(hours=23))

    with pytest.raises(BudgetExceeded) as blocked:
        reserve(session, current, "0.40")

    assert [v.limit for v in blocked.value.violations] == ["daily"]
    assert reserve(session, current, "0.20").status is LedgerStatus.RESERVED


def test_monthly_ceiling_counts_only_the_last_30_days(session: Session) -> None:
    ancient, month, current = (make_experiment(session) for _ in range(3))
    spend(session, ancient, "400.00", at=NOW - timedelta(days=31))
    spend(session, month, "499.90", at=NOW - timedelta(days=29))

    with pytest.raises(BudgetExceeded) as blocked:
        reserve(session, current, "0.40")

    assert [v.limit for v in blocked.value.violations] == ["monthly"]


def test_settled_actual_cost_replaces_the_estimate_in_committed_spend(session: Session) -> None:
    experiment = make_experiment(session)
    entry = reserve(session, experiment, "0.50")

    governor().settle(session, entry, actual_cost_usd=Decimal("0.30"))

    assert entry.status is LedgerStatus.SETTLED
    assert entry.settled_at == NOW
    assert governor().committed_spend_for_experiment(session, experiment.id) == Decimal("0.30")


def test_settling_without_a_reported_cost_keeps_counting_the_estimate(session: Session) -> None:
    experiment = make_experiment(session)
    entry = reserve(session, experiment, "0.50")

    governor().settle(session, entry, actual_cost_usd=None)

    assert entry.status is LedgerStatus.SETTLED
    assert entry.actual_cost_usd is None
    assert governor().committed_spend_for_experiment(session, experiment.id) == Decimal("0.50")


def test_an_entry_cannot_be_settled_twice(session: Session) -> None:
    experiment = make_experiment(session)
    entry = reserve(session, experiment, "0.50")
    governor().settle(session, entry, actual_cost_usd=Decimal("0.30"))

    with pytest.raises(ValueError, match="already settled"):
        governor().settle(session, entry, actual_cost_usd=Decimal("0.10"))
