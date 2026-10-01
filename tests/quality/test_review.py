from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.lineage import get_lineage
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import HumanReview, ReviewDecision
from app.quality.ports import QAOutcome
from app.quality.review import ReviewNotAllowed, pending_reviews, submit_review
from app.quality.runner import run_quality_gates
from integrations.fake.quality import FakeQAGate
from integrations.object_storage.local import LocalAssetStore
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


# --- escalations and flags ---------------------------------------------------


def escalated_experiment(session: Session, tmp_path: Path) -> Experiment:
    store = LocalAssetStore(tmp_path / "assets")
    experiment = make_generated_experiment(session, tmp_path, store=store)
    lookalike = FakeQAGate(
        name="ip_brand", mandatory=True, outcome=QAOutcome.ESCALATE, reasons=["resembles Sonic"]
    )
    run_quality_gates(session, experiment.id, gates=[lookalike], store=store)
    session.expire_all()
    return experiment


def test_approving_an_escalated_video_requires_a_written_resolution(
    session: Session, tmp_path: Path
) -> None:
    experiment = escalated_experiment(session, tmp_path)

    with pytest.raises(ValueError, match="escalat"):
        submit_review(
            session, experiment.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior"
        )
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING

    submit_review(
        session,
        experiment.id,
        decision=ReviewDecision.APPROVE,
        reason="Checked side by side: silhouette and colours are clearly different.",
        reviewer="lior",
    )
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.READY


def test_flagging_keeps_the_video_pending_and_records_why(session: Session, tmp_path: Path) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)

    review = submit_review(
        session,
        experiment.id,
        decision=ReviewDecision.FLAG,
        reason="Unsure about the music; want a second look.",
        reviewer="lior",
    )

    assert review.decision is ReviewDecision.FLAG
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
    assert [e.id for e in pending_reviews(session)] == [experiment.id]


def test_a_flag_needs_a_reason_and_can_be_followed_by_a_decision(
    session: Session, tmp_path: Path
) -> None:
    experiment = make_reviewable_experiment(session, tmp_path)

    with pytest.raises(ValueError, match="reason"):
        submit_review(
            session, experiment.id, decision=ReviewDecision.FLAG, reason="", reviewer="lior"
        )
    submit_review(
        session, experiment.id, decision=ReviewDecision.FLAG, reason="Check audio.", reviewer="lior"
    )
    submit_review(
        session, experiment.id, decision=ReviewDecision.APPROVE, reason="Audio is fine.",
        reviewer="lior",
    )  # fmt: skip

    decisions = [
        r.decision for r in session.scalars(select(HumanReview).order_by(HumanReview.created_at))
    ]
    assert decisions == [ReviewDecision.FLAG, ReviewDecision.APPROVE]
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.READY
