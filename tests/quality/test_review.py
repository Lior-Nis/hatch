from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.lineage import get_lineage
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import HumanReview, ReviewDecision
from app.quality.review import ReviewNotAllowed, pending_reviews, submit_review
from tests.factories import (
    final_asset,
    make_experiment,
    make_generated_experiment,
    make_reviewable_experiment,
)


def test_approval_is_persisted_with_reviewer_reason_and_asset(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)

    review = submit_review(
        session,
        experiment.id,
        decision=ReviewDecision.APPROVE,
        reason="Gentle, clear, on-model.",
        reviewer="lior",
    )

    session.expire_all()
    stored = session.get_one(HumanReview, review.id)
    assert stored.decision is ReviewDecision.APPROVE
    assert stored.reason == "Gentle, clear, on-model."
    assert stored.reviewer == "lior"
    assert stored.asset_id == final_asset(session.get_one(Experiment, experiment.id)).id
    assert stored.created_at.tzinfo is not None
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.READY


def test_rejection_moves_the_video_to_human_rejected(session: Session, tmp_path: Path) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)

    submit_review(
        session,
        experiment.id,
        decision=ReviewDecision.REJECT,
        reason="Character drifts off-model at 0:05.",
        reviewer="lior",
    )

    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.HUMAN_REJECTED


@pytest.mark.parametrize("reason", ["", "   "])
def test_rejection_without_a_reason_is_refused(
    session: Session, tmp_path: Path, reason: str
) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)

    with pytest.raises(ValueError, match="reason"):
        submit_review(
            session, experiment.id, decision=ReviewDecision.REJECT, reason=reason, reviewer="lior"
        )

    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
    assert session.scalars(select(HumanReview)).all() == []


def test_a_video_that_has_not_passed_automated_qa_cannot_be_reviewed(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_generated_experiment(session, tmp_path)

    with pytest.raises(ReviewNotAllowed):
        submit_review(
            session, experiment.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior"
        )


def test_a_decided_video_cannot_be_reviewed_again(session: Session, tmp_path: Path) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)
    submit_review(
        session, experiment.id, decision=ReviewDecision.REJECT, reason="Too dark.", reviewer="lior"
    )

    with pytest.raises(ReviewNotAllowed):
        submit_review(
            session, experiment.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior"
        )

    assert len(session.scalars(select(HumanReview)).all()) == 1


def test_pending_reviews_lists_only_videos_awaiting_a_decision(
    session: Session, tmp_path: Path
) -> None:
    waiting = make_reviewable_experiment(session, tmp_path)
    decided = make_reviewable_experiment(session, tmp_path)
    make_experiment(session)  # never generated
    submit_review(session, decided.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior")

    assert [experiment.id for experiment in pending_reviews(session)] == [waiting.id]


def test_review_decisions_appear_in_the_lineage(session: Session, tmp_path: Path) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)
    submit_review(
        session, experiment.id, decision=ReviewDecision.REJECT, reason="Too dark.", reviewer="lior"
    )

    [review] = get_lineage(session, experiment.id).human_reviews

    assert (review.decision, review.reason, review.reviewer) == ("reject", "Too dark.", "lior")
