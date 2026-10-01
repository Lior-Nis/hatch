from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.budgets.governor import BudgetLimits
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import evaluate_replication, request_replication
from app.pilot.report import pilot_report, render_memo
from tests.evolution.helpers import evaluated
from tests.factories import LIMITS

POLICY = EvolutionPolicy()
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def spend(session: Session, experiment_id: object, amount: str) -> None:
    session.add(
        BudgetLedgerEntry(
            provider="higgsfield",
            model="m",
            operation="text_to_video",
            experiment_id=experiment_id,
            estimated_cost_usd=Decimal(amount),
            actual_cost_usd=Decimal(amount),
            status=LedgerStatus.SETTLED,
            created_at=datetime(2026, 10, 20, tzinfo=UTC),
        )
    )
    session.flush()


def test_report_answers_the_pilot_questions_from_data(session: Session) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    for _ in range(3):
        evaluated(session, 0.7, parent=winner)
    evaluate_replication(session, winner, POLICY)
    spend(session, winner.id, "0.40")

    report = pilot_report(session, limits=LIMITS, now=NOW)

    answers = {q.key: q for q in report.questions}
    assert answers["within_budget"].answer is True
    assert "$0.40 of $500.00" in answers["within_budget"].evidence
    assert answers["reproduced_hypothesis"].answer is True
    assert "1 hypothesis" in answers["reproduced_hypothesis"].evidence
    assert answers["publishes_consistently"].answer is False  # nothing was published here
    assert report.replication_verdicts == {"supported": 1}
    assert report.recommendation in ("scale", "iterate", "stop")


def test_an_overspent_pilot_recommends_stopping(session: Session) -> None:
    experiment = evaluated(session, 0.5)
    spend(session, experiment.id, "600.00")

    report = pilot_report(
        session,
        limits=BudgetLimits(
            max_per_generation_usd=Decimal("1000"),
            max_per_video_usd=Decimal("1000"),
            daily_usd=Decimal("1000"),
            monthly_usd=Decimal("500"),
        ),
        now=NOW,
    )

    assert {q.key: q.answer for q in report.questions}["within_budget"] is False
    assert report.recommendation == "stop"


def test_an_empty_pilot_recommends_iterating_not_scaling(session: Session) -> None:
    report = pilot_report(session, limits=LIMITS, now=NOW)

    assert report.recommendation == "iterate"
    assert all(q.answer is not True or q.key == "within_budget" for q in report.questions)


def test_memo_is_readable_markdown_with_the_decision_left_to_the_operator(
    session: Session,
) -> None:
    evaluated(session, 0.6)

    memo = render_memo(pilot_report(session, limits=LIMITS, now=NOW))

    assert memo.startswith("# Hatch 30-day pilot review")
    assert "## The seven pilot questions" in memo
    assert "Recommendation from the data" in memo
    assert "Decision (yours)" in memo
