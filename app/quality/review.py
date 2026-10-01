"""Human review of generated videos (Stage A: every video is approved by a
person before it can become READY).

Decisions are append-only evidence: who decided, on which asset, when, and why.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.models import Asset, AssetKind
from app.quality.models import HumanReview, QAResult, ReviewDecision
from app.quality.ports import QAOutcome

# Only a video that has been through automated QA without a mandatory failure
# reaches a human. A QA_REJECTED video can never be approved.
REVIEWABLE = (VideoStatus.APPROVAL_PENDING,)


class ReviewNotAllowed(Exception):
    """The experiment's video is not awaiting a human decision."""


def pending_reviews(session: Session) -> list[Experiment]:
    return list(
        session.scalars(
            select(Experiment)
            .where(Experiment.video_status.in_(REVIEWABLE))
            .order_by(Experiment.created_at)
        )
    )


def final_video(session: Session, experiment_id: uuid.UUID) -> Asset | None:
    return session.scalars(
        select(Asset)
        .where(Asset.experiment_id == experiment_id, Asset.kind == AssetKind.FINAL_VIDEO)
        .order_by(Asset.created_at.desc())
    ).first()


def submit_review(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    decision: ReviewDecision,
    reason: str,
    reviewer: str,
) -> HumanReview:
    reason = reason.strip()
    if decision in (ReviewDecision.REJECT, ReviewDecision.FLAG) and not reason:
        raise ValueError(f"a reason is required to {decision.value} a video")

    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    asset = final_video(session, experiment_id)
    if experiment.video_status not in REVIEWABLE or asset is None:
        raise ReviewNotAllowed(
            f"experiment {experiment_id} is not awaiting review "
            f"(video is {experiment.video_status.value})"
        )
    if decision is ReviewDecision.APPROVE and not reason and escalations(session, asset.id):
        # A flagged case stays blocked until a human resolves it in writing.
        raise ValueError(
            "automated QA escalated this video: a reason is required to approve it, "
            "explaining how each flag was resolved"
        )

    review = HumanReview(
        experiment=experiment, asset=asset, decision=decision, reason=reason, reviewer=reviewer
    )
    session.add(review)
    if decision is ReviewDecision.APPROVE:
        experiment.video_status = VideoStatus.READY
    elif decision is ReviewDecision.REJECT:
        experiment.video_status = VideoStatus.HUMAN_REJECTED
    session.flush()
    return review


def escalations(session: Session, asset_id: uuid.UUID) -> list[QAResult]:
    """QA results on this asset that need a human decision."""
    return list(
        session.scalars(
            select(QAResult)
            .where(QAResult.asset_id == asset_id, QAResult.outcome == QAOutcome.ESCALATE)
            .order_by(QAResult.created_at)
        )
    )
