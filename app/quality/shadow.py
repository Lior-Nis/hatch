"""Shadow autonomy: would the system have decided what the human decided?

For every video a human has approved or rejected, Hatch derives what the
automated gates alone would have done with it and compares:

    system approve + human approve   → agreement
    system approve + human reject    → FALSE POSITIVE (it would have published
                                       something a person stopped)
    system escalate + human approve  → false negative (over-cautious)
    system escalate + human reject   → agreement

Automated rejections are never shown to a reviewer for approval, so they are
measured separately through audits of a sample.

This module only measures. It contains no switch: publishing without a human
approval is not implemented, and meeting the thresholds here does not enable
it. Stage C requires the operator's explicit decision.
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.quality.models import HumanReview, QAResult, ReviewDecision
from app.quality.ports import QAOutcome

Prediction = Literal["approve", "escalate", "reject"]

_DECISIONS = (ReviewDecision.APPROVE, ReviewDecision.REJECT)
_AUDITS = (ReviewDecision.AUDIT_AGREE, ReviewDecision.AUDIT_DISAGREE)


class AutonomyThresholds(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_compared: int = 300
    """Stage A (~100 videos) plus Stage B (~200) of human-reviewed decisions."""
    max_false_positive_rate: float = 0.01


class Disagreement(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: uuid.UUID
    kind: Literal["false_positive", "false_negative", "wrong_rejection"]
    category: str
    human_reason: str
    decided_at: datetime


class ShadowReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    compared: int
    agreements: int
    false_positives: int
    false_negatives: int
    agreement_rate: float | None
    false_positive_rate: float | None
    """Of the videos the system would have approved, the share a human rejected."""
    false_negative_rate: float | None
    """Of the videos a human approved, the share the system held back."""
    auto_rejected: int
    audited_rejections: int
    wrong_rejections: int
    disagreement_categories: dict[str, int]
    disagreements: list[Disagreement]
    meets_thresholds: bool
    blockers: list[str]
    note: str = (
        "Meeting these thresholds does not enable autonomous publishing: Stage C needs the "
        "operator's explicit approval, and no code path publishes without a human approval."
    )


def predict_decision(results: Sequence[QAResult]) -> Prediction:
    """What automated QA alone would do with a video."""
    if not results:
        return "escalate"
    if any(r.mandatory and r.outcome is QAOutcome.FAIL for r in results):
        return "reject"
    if any(r.outcome is not QAOutcome.PASS for r in results):
        return "escalate"
    return "approve"


def _gates(results: Sequence[QAResult], outcome: QAOutcome) -> str:
    return "+".join(sorted({r.gate for r in results if r.outcome is outcome})) or "none"


def shadow_autonomy_report(
    session: Session, *, thresholds: AutonomyThresholds | None = None
) -> ShadowReport:
    thresholds = thresholds or AutonomyThresholds()
    qa_by_asset: dict[uuid.UUID, list[QAResult]] = {}
    for result in session.scalars(select(QAResult).order_by(QAResult.created_at)):
        qa_by_asset.setdefault(result.asset_id, []).append(result)

    # The final human decision per asset (a later decision supersedes none:
    # approve/reject is terminal, but be explicit about taking the latest).
    decisions: dict[uuid.UUID, HumanReview] = {}
    audits: dict[uuid.UUID, HumanReview] = {}
    for review in session.scalars(select(HumanReview).order_by(HumanReview.created_at)):
        if review.decision in _DECISIONS:
            decisions[review.asset_id] = review
        elif review.decision in _AUDITS:
            audits[review.asset_id] = review

    agreements = false_positives = false_negatives = system_approvals = human_approvals = 0
    disagreements: list[Disagreement] = []
    for asset_id, review in decisions.items():
        results = qa_by_asset.get(asset_id, [])
        prediction = predict_decision(results)
        approved = review.decision is ReviewDecision.APPROVE
        system_approvals += prediction == "approve"
        human_approvals += approved
        if (prediction == "approve") == approved:
            agreements += 1
            continue
        if prediction == "approve":
            false_positives += 1
            kind: Literal["false_positive", "false_negative"] = "false_positive"
            category = "missed_by_all_gates"
        else:
            false_negatives += 1
            kind = "false_negative"
            category = f"over_escalated:{_gates(results, QAOutcome.ESCALATE)}"
        disagreements.append(
            Disagreement(
                experiment_id=review.experiment_id,
                kind=kind,
                category=category,
                human_reason=review.reason,
                decided_at=review.created_at,
            )
        )

    auto_rejected = sum(
        1 for results in qa_by_asset.values() if predict_decision(results) == "reject"
    )
    wrong_rejections = 0
    for asset_id, audit in audits.items():
        if audit.decision is ReviewDecision.AUDIT_DISAGREE:
            wrong_rejections += 1
            disagreements.append(
                Disagreement(
                    experiment_id=audit.experiment_id,
                    kind="wrong_rejection",
                    category=(
                        f"wrongly_rejected:{_gates(qa_by_asset.get(asset_id, []), QAOutcome.FAIL)}"
                    ),
                    human_reason=audit.reason,
                    decided_at=audit.created_at,
                )
            )

    compared = len(decisions)
    false_positive_rate = false_positives / system_approvals if system_approvals else None
    blockers = []
    if compared < thresholds.min_compared:
        blockers.append(
            f"only {compared} human decisions to compare with; {thresholds.min_compared} are needed"
        )
    if false_positive_rate is not None and false_positive_rate > thresholds.max_false_positive_rate:
        blockers.append(
            f"false positive rate {false_positive_rate:.1%} is above "
            f"{thresholds.max_false_positive_rate:.1%}: the system would have published "
            f"{false_positives} video(s) a human rejected"
        )
    return ShadowReport(
        compared=compared,
        agreements=agreements,
        false_positives=false_positives,
        false_negatives=false_negatives,
        agreement_rate=agreements / compared if compared else None,
        false_positive_rate=false_positive_rate,
        false_negative_rate=false_negatives / human_approvals if human_approvals else None,
        auto_rejected=auto_rejected,
        audited_rejections=len(audits),
        wrong_rejections=wrong_rejections,
        disagreement_categories=dict(Counter(d.category for d in disagreements)),
        disagreements=disagreements,
        meets_thresholds=not blockers,
        blockers=blockers,
    )
