from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.budgets.reports import spend_report
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from tests.factories import force_video_status, make_experiment

DAY_1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
DAY_2 = datetime(2026, 10, 2, 23, 30, tzinfo=UTC)


def charge(
    session: Session,
    experiment: Experiment | None,
    *,
    estimated: str,
    actual: str | None = None,
    provider: str = "higgsfield",
    at: datetime = DAY_1,
    status: LedgerStatus = LedgerStatus.SETTLED,
) -> None:
    session.add(
        BudgetLedgerEntry(
            provider=provider,
            model="m",
            operation="text_to_video",
            experiment_id=experiment.id if experiment else None,
            estimated_cost_usd=Decimal(estimated),
            actual_cost_usd=Decimal(actual) if actual is not None else None,
            status=status,
            created_at=at,
        )
    )
    session.flush()


def test_spend_is_reported_by_video_ip_provider_and_day(session: Session) -> None:
    nib_a = make_experiment(session)
    nib_b = make_experiment(session)
    zed = make_experiment(session, ip_slug="zed-lab")
    charge(session, nib_a, estimated="0.40", actual="0.38")
    charge(session, nib_a, estimated="0.40", at=DAY_2)  # retry, cost not reported → estimate
    charge(session, nib_b, estimated="0.50", actual="0.50", provider="other")
    charge(session, zed, estimated="0.20", actual="0.25", at=DAY_2)

    report = spend_report(session)

    assert report.total_usd == Decimal("1.53")
    assert report.by_experiment == {
        nib_a.id: Decimal("0.78"),
        nib_b.id: Decimal("0.50"),
        zed.id: Decimal("0.25"),
    }
    assert report.by_ip == {"nibbin-hollow": Decimal("1.28"), "zed-lab": Decimal("0.25")}
    assert report.by_provider == {"higgsfield": Decimal("1.03"), "other": Decimal("0.50")}
    assert report.by_day == {date(2026, 10, 1): Decimal("0.88"), date(2026, 10, 2): Decimal("0.65")}


def test_blocked_attempts_are_not_spend(session: Session) -> None:
    experiment = make_experiment(session)
    charge(session, experiment, estimated="0.40", actual="0.40")
    charge(session, experiment, estimated="9.00", status=LedgerStatus.BLOCKED)

    report = spend_report(session)

    assert report.total_usd == Decimal("0.40")
    assert report.blocked_attempts == 1


def test_cost_per_accepted_video_includes_the_spend_wasted_on_rejected_ones(
    session: Session,
) -> None:
    accepted, published, rejected, failed = (make_experiment(session) for _ in range(4))
    force_video_status(session, accepted, VideoStatus.READY)
    force_video_status(session, published, VideoStatus.PUBLISHED)
    force_video_status(session, rejected, VideoStatus.HUMAN_REJECTED)
    force_video_status(session, failed, VideoStatus.GENERATION_FAILED)
    for experiment, amount in [(accepted, "0.40"), (published, "0.40"), (rejected, "0.80")]:
        charge(session, experiment, estimated=amount, actual=amount)
    charge(session, failed, estimated="0.40", actual="0.00")

    report = spend_report(session)

    assert report.total_usd == Decimal("1.60")
    assert (report.videos, report.accepted_videos, report.published_videos) == (4, 2, 1)
    assert report.cost_per_video_usd == Decimal("0.40")
    assert report.cost_per_accepted_video_usd == Decimal("0.80")
    assert report.cost_per_published_video_usd == Decimal("1.60")


def test_ratios_are_undefined_rather_than_zero_when_nothing_qualifies(session: Session) -> None:
    experiment = make_experiment(session)
    charge(session, experiment, estimated="0.40", actual="0.40")

    report = spend_report(session)

    assert report.cost_per_video_usd == Decimal("0.40")
    assert report.cost_per_accepted_video_usd is None
    assert report.cost_per_published_video_usd is None


def test_report_can_be_limited_to_a_time_window(session: Session) -> None:
    experiment = make_experiment(session)
    charge(session, experiment, estimated="0.40", actual="0.40", at=DAY_1)
    charge(session, experiment, estimated="0.30", actual="0.30", at=DAY_2)

    report = spend_report(session, since=datetime(2026, 10, 2, tzinfo=UTC))

    assert report.total_usd == Decimal("0.30")
    assert list(report.by_day) == [date(2026, 10, 2)]


def test_spend_without_an_experiment_still_counts_towards_totals(session: Session) -> None:
    charge(session, None, estimated="0.10", actual="0.10")

    report = spend_report(session)

    assert report.total_usd == Decimal("0.10")
    assert report.by_experiment == {}
    assert report.videos == 0
