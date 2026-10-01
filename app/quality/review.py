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
from app.quality.models import HumanReview, ReviewDecision

# Until automated QA gates exist, a generated video goes straight to a human.
# Once they do, only APPROVAL_PENDING (i.e. QA already run) may be reviewed.
REVIEWABLE = (VideoStatus.GENERATED, VideoStatus.APPROVAL_PENDING)


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
    if decision is ReviewDecision.REJECT and not reason:
        raise ValueError("a reason is required to reject a video")

    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    asset = final_video(session, experiment_id)
    if experiment.video_status not in REVIEWABLE or asset is None:
        raise ReviewNotAllowed(
            f"experiment {experiment_id} is not awaiting review "
            f"(video is {experiment.video_status.value})"
        )

    review = HumanReview(
        experiment=experiment, asset=asset, decision=decision, reason=reason, reviewer=reviewer
    )
    session.add(review)
    experiment.video_status = (
        VideoStatus.READY if decision is ReviewDecision.APPROVE else VideoStatus.HUMAN_REJECTED
    )
    session.flush()
    return review
