"""30-day pilot review: the data behind a scale / iterate / stop decision.

The PRD judges the first month on seven questions. This module answers each
from stored evidence and proposes a recommendation from fixed rules. The
decision itself is the operator's.
"""

from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.audit import CompletenessReport, analytics_completeness
from app.budgets.governor import BudgetLimits
from app.budgets.reports import SpendReport, spend_report
from app.db import utcnow
from app.evolution.models import AllocationBucket, DecisionType, SelectionDecision
from app.experiments.models import Experiment
from app.ips.models import IP
from app.knowledge.models import KnowledgeSummary
from app.publishing.models import Publication, PublicationRecordStatus
from app.quality.models import HumanReview, ReviewDecision
from app.quality.shadow import ShadowReport, shadow_autonomy_report

MIN_VIDEOS = 10
"""Fewer accepted or published videos than this says little about reliability."""


class PilotQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    question: str
    answer: bool
    evidence: str


class PilotReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    period_start: datetime
    period_end: datetime
    spend: SpendReport
    questions: list[PilotQuestion]
    replication_verdicts: dict[str, int]
    ip_statuses: dict[str, str]
    allocation: dict[str, int]
    reviews: dict[str, int]
    analytics: CompletenessReport
    autonomy: ShadowReport
    recommendation: Literal["scale", "iterate", "stop"]
    rationale: str


def pilot_report(
    session: Session, *, limits: BudgetLimits, now: datetime | None = None, days: int = 30
) -> PilotReport:
    now = now or utcnow()
    start = now - timedelta(days=days)
    spend = spend_report(session, since=start, until=now)
    analytics = analytics_completeness(session, now=now)
    autonomy = shadow_autonomy_report(session)

    publications = Counter(
        session.scalars(select(Publication.status).where(Publication.created_at >= start))
    )
    published = publications[PublicationRecordStatus.PUBLISHED]
    publish_failed = publications[PublicationRecordStatus.FAILED]
    replicated = select(SelectionDecision.subject_experiment_id).where(
        SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION
    )
    verdicts = Counter(
        conclusion.value
        for conclusion in session.scalars(
            select(Experiment.conclusion).where(
                Experiment.id.in_(replicated), Experiment.conclusion.is_not(None)
            )
        )
        if conclusion is not None
    )
    supported = verdicts.get("supported", 0)
    allocation = Counter(
        bucket.value
        for bucket in session.scalars(
            select(SelectionDecision.bucket).where(
                SelectionDecision.decision_type == DecisionType.CREATE_EXPERIMENT,
                SelectionDecision.resulting_experiment_id.is_not(None),
            )
        )
        if bucket is not None
    )
    findings = session.scalar(
        select(func.count())
        .select_from(KnowledgeSummary)
        .where(KnowledgeSummary.topic.like("gene:%"), KnowledgeSummary.confidence >= 0.3)
    )
    reviews = Counter(decision.value for decision in session.scalars(select(HumanReview.decision)))
    decided = reviews[ReviewDecision.APPROVE.value] + reviews[ReviewDecision.REJECT.value]

    acceptance = spend.accepted_videos / spend.videos if spend.videos else 0.0
    publish_rate = published / (published + publish_failed) if published + publish_failed else 0.0
    per_accepted = (
        f"${spend.cost_per_accepted_video_usd:.2f}"
        if spend.cost_per_accepted_video_usd is not None
        else "n/a"
    )
    questions = [
        PilotQuestion(
            key="acceptable_content",
            question="Can Hatch reliably produce acceptable content?",
            answer=spend.accepted_videos >= MIN_VIDEOS and acceptance >= 0.5,
            evidence=(
                f"{spend.accepted_videos} of {spend.videos} produced videos were accepted "
                f"({acceptance:.0%}); {decided} human decisions, "
                f"{reviews[ReviewDecision.REJECT.value]} rejections."
            ),
        ),
        PilotQuestion(
            key="publishes_consistently",
            question="Can it publish consistently?",
            answer=spend.published_videos >= MIN_VIDEOS and publish_rate >= 0.9,
            evidence=(
                f"{spend.published_videos} videos published; {published} platform posts "
                f"succeeded and {publish_failed} failed ({publish_rate:.0%})."
            ),
        ),
        PilotQuestion(
            key="measures_correctly",
            question="Can it measure performance correctly?",
            answer=analytics.due > 0 and analytics.missing == 0,
            evidence=(
                f"{analytics.collected} of {analytics.due} due observations collected, "
                f"{analytics.failed} explicit failures, {analytics.missing} silent gaps."
            ),
        ),
        PilotQuestion(
            key="detects_differences",
            question="Can it detect meaningful concept differences?",
            answer=bool(findings) or bool(verdicts),
            evidence=(
                f"{findings or 0} gene-level findings with a clear difference; "
                f"{sum(verdicts.values())} replication verdicts."
            ),
        ),
        PilotQuestion(
            key="reproduced_hypothesis",
            question="Has it reproduced at least one successful creative hypothesis?",
            answer=supported >= 1,
            evidence=(
                f"{supported} hypothes{'is' if supported == 1 else 'es'} reproduced under "
                f"replication; all verdicts: {dict(verdicts) or 'none'}."
            ),
        ),
        PilotQuestion(
            key="uses_findings",
            question="Does it use findings to improve subsequent production?",
            answer=allocation.get(AllocationBucket.EXPLOIT.value, 0) >= 1,
            evidence=(
                f"candidates created by bucket: {dict(allocation) or 'none'} "
                "(exploit means a replicated lineage received more production)."
            ),
        ),
        PilotQuestion(
            key="within_budget",
            question="Did it stay inside the approved budget?",
            answer=spend.total_usd <= limits.monthly_usd,
            evidence=(
                f"${spend.total_usd:.2f} of ${limits.monthly_usd:.2f} spent in {days} days; "
                f"cost per accepted video {per_accepted}; "
                f"{spend.blocked_attempts} attempts blocked by the governor."
            ),
        ),
    ]
    answers = {q.key: q.answer for q in questions}
    unmet = [q.question for q in questions if not q.answer]
    core = ("acceptable_content", "publishes_consistently", "measures_correctly",
            "reproduced_hypothesis", "within_budget")  # fmt: skip
    recommendation: Literal["scale", "iterate", "stop"]
    if not answers["within_budget"]:
        recommendation = "stop"
        rationale = (
            "The pilot exceeded its approved budget; do not continue without a new decision."
        )
    elif all(answers[key] for key in core):
        recommendation = "scale"
        rationale = (
            "Content, publishing, measurement and budget held, and at least one hypothesis "
            "reproduced under replication."
        )
    else:
        recommendation = "iterate"
        rationale = "Not yet shown: " + " ".join(unmet)
    return PilotReport(
        period_start=start,
        period_end=now,
        spend=spend,
        questions=questions,
        replication_verdicts=dict(verdicts),
        ip_statuses={ip.slug: ip.status.value for ip in session.scalars(select(IP))},
        allocation=dict(allocation),
        reviews=dict(reviews),
        analytics=analytics,
        autonomy=autonomy,
        recommendation=recommendation,
        rationale=rationale,
    )


def render_memo(report: PilotReport) -> str:
    lines = [
        "# Hatch 30-day pilot review",
        "",
        f"Period: {report.period_start:%Y-%m-%d} to {report.period_end:%Y-%m-%d} (UTC).",
        "",
        "## The seven pilot questions",
        "",
        "| Question | Answer | Evidence |",
        "|---|---|---|",
    ]
    lines += [
        f"| {q.question} | {'yes' if q.answer else 'no'} | {q.evidence} |" for q in report.questions
    ]
    spend = report.spend

    def money(value: object) -> str:
        return f"${value:.2f}" if value is not None else "n/a"

    lines += [
        "",
        "## Economics",
        "",
        f"- Total spend: {money(spend.total_usd)}",
        f"- Cost per video / accepted / published: {money(spend.cost_per_video_usd)} / "
        f"{money(spend.cost_per_accepted_video_usd)} / {money(spend.cost_per_published_video_usd)}",
        f"- Spend by IP: {', '.join(f'{k} {money(v)}' for k, v in spend.by_ip.items()) or 'none'}",
        f"- Spend by provider: "
        f"{', '.join(f'{k} {money(v)}' for k, v in spend.by_provider.items()) or 'none'}",
        "",
        "## Evidence",
        "",
        f"- IP status: {report.ip_statuses or 'no IPs'}",
        f"- Replication verdicts: {report.replication_verdicts or 'none'}",
        f"- Candidates created by allocation bucket: {report.allocation or 'none'}",
        f"- Analytics: {report.analytics.collected}/{report.analytics.due} observations "
        f"collected, {report.analytics.failed} failed, {report.analytics.missing} missing",
        "",
        "## Human review burden and autonomy",
        "",
        f"- Review decisions: {report.reviews or 'none'}",
        f"- Shadow autonomy: {report.autonomy.compared} decisions compared, "
        f"{report.autonomy.false_positives} false positives, "
        f"{report.autonomy.false_negatives} false negatives; thresholds met: "
        f"{'yes' if report.autonomy.meets_thresholds else 'no'}",
        "",
        "## Recommendation from the data",
        "",
        f"**{report.recommendation.upper()}** — {report.rationale}",
        "",
        "## Decision (yours)",
        "",
        "- [ ] Scale   - [ ] Iterate   - [ ] Stop",
        "",
        "Notes:",
        "",
    ]
    return "\n".join(lines)
