from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import ReviewDecision
from app.quality.ports import QAOutcome
from app.quality.review import ReviewNotAllowed, audit_rejection, submit_review
from app.quality.runner import run_quality_gates
from app.quality.shadow import AutonomyThresholds, predict_decision, shadow_autonomy_report
from integrations.fake.quality import FakeQAGate
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_generated_experiment


def gate(name: str, outcome: QAOutcome, *reasons: str) -> FakeQAGate:
    return FakeQAGate(name=name, mandatory=True, outcome=outcome, reasons=list(reasons))


def reviewed(
    session: Session,
    tmp_path: Path,
    gates: list[FakeQAGate],
    decision: ReviewDecision | None,
    reason: str = "",
) -> Experiment:
    store = LocalAssetStore(tmp_path / "assets")
    experiment = make_generated_experiment(session, tmp_path, store=store)
    run_quality_gates(session, experiment.id, gates=gates, store=store)
    if decision is not None:
        submit_review(session, experiment.id, decision=decision, reason=reason, reviewer="lior")
    session.expire_all()
    return experiment


CLEAN = [gate("technical", QAOutcome.PASS), gate("child_safety", QAOutcome.PASS)]
UNSURE = [gate("technical", QAOutcome.PASS), gate("ip_brand", QAOutcome.ESCALATE, "resembles X")]
UNSAFE = [gate("child_safety", QAOutcome.FAIL, "frightening imagery")]


def test_the_system_would_approve_only_a_video_that_passes_every_gate(
    session: Session, tmp_path: Path
) -> None:
    clean = reviewed(session, tmp_path, CLEAN, None)
    unsure = reviewed(session, tmp_path, UNSURE, None)
    unsafe = reviewed(session, tmp_path, UNSAFE, None)

    assert predict_decision(clean.qa_results) == "approve"
    assert predict_decision(unsure.qa_results) == "escalate"
    assert predict_decision(unsafe.qa_results) == "reject"
    assert predict_decision([]) == "escalate"  # no QA is never an approval


def test_report_counts_agreement_false_positives_and_false_negatives(
    session: Session, tmp_path: Path
) -> None:
    reviewed(session, tmp_path, CLEAN, ReviewDecision.APPROVE)
    reviewed(session, tmp_path, CLEAN, ReviewDecision.APPROVE)
    reviewed(session, tmp_path, CLEAN, ReviewDecision.REJECT, "Ending is confusing.")
    reviewed(session, tmp_path, UNSURE, ReviewDecision.APPROVE, "Checked: not similar.")
    reviewed(session, tmp_path, UNSURE, ReviewDecision.REJECT, "Yes, too close to X.")
    reviewed(session, tmp_path, CLEAN, None)  # not decided yet: not compared

    report = shadow_autonomy_report(session)

    assert report.compared == 5
    assert report.agreements == 3
    assert report.false_positives == 1  # system would have published; the human rejected
    assert report.false_negatives == 1  # system held back; the human approved
    assert report.agreement_rate == pytest.approx(0.6)
    assert report.false_positive_rate == pytest.approx(1 / 3)  # of the system's approvals


def test_disagreements_are_grouped_into_categories(session: Session, tmp_path: Path) -> None:
    reviewed(session, tmp_path, CLEAN, ReviewDecision.REJECT, "Ending is confusing.")
    reviewed(session, tmp_path, UNSURE, ReviewDecision.APPROVE, "Checked: not similar.")
    reviewed(session, tmp_path, UNSURE, ReviewDecision.APPROVE, "Fine.")

    report = shadow_autonomy_report(session)

    assert report.disagreement_categories == {
        "missed_by_all_gates": 1,
        "over_escalated:ip_brand": 2,
    }
    missed = next(d for d in report.disagreements if d.kind == "false_positive")
    assert missed.human_reason == "Ending is confusing."


def test_flags_do_not_count_as_decisions(session: Session, tmp_path: Path) -> None:
    experiment = reviewed(session, tmp_path, CLEAN, ReviewDecision.FLAG, "Look again later.")

    assert shadow_autonomy_report(session).compared == 0

    submit_review(
        session, experiment.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior"
    )
    assert shadow_autonomy_report(session).compared == 1


def test_automated_rejections_can_be_audited_without_changing_the_video(
    session: Session, tmp_path: Path
) -> None:
    rejected = reviewed(session, tmp_path, UNSAFE, None)
    wrongly = reviewed(session, tmp_path, UNSAFE, None)

    audit_rejection(session, rejected.id, agrees=True, reason="Yes, scary.", reviewer="lior")
    audit_rejection(
        session, wrongly.id, agrees=False, reason="It is just a shadow.", reviewer="lior"
    )

    session.expire_all()
    assert session.get_one(Experiment, wrongly.id).video_status is VideoStatus.QA_REJECTED
    report = shadow_autonomy_report(session)
    assert (report.auto_rejected, report.audited_rejections, report.wrong_rejections) == (2, 2, 1)
    assert report.disagreement_categories == {"wrongly_rejected:child_safety": 1}


def test_only_rejected_videos_can_be_audited(session: Session, tmp_path: Path) -> None:
    pending = reviewed(session, tmp_path, CLEAN, None)

    with pytest.raises(ReviewNotAllowed):
        audit_rejection(session, pending.id, agrees=True, reason="x", reviewer="lior")


def test_autonomy_is_not_earned_without_enough_agreeing_evidence(
    session: Session, tmp_path: Path
) -> None:
    for _ in range(3):
        reviewed(session, tmp_path, CLEAN, ReviewDecision.APPROVE)

    report = shadow_autonomy_report(session)

    assert report.meets_thresholds is False
    assert any("300" in blocker for blocker in report.blockers)


def test_thresholds_are_met_only_with_enough_samples_and_no_false_positives(
    session: Session, tmp_path: Path
) -> None:
    thresholds = AutonomyThresholds(min_compared=4, max_false_positive_rate=0.0)
    for _ in range(4):
        reviewed(session, tmp_path, CLEAN, ReviewDecision.APPROVE)

    assert shadow_autonomy_report(session, thresholds=thresholds).meets_thresholds is True

    reviewed(session, tmp_path, CLEAN, ReviewDecision.REJECT, "Off-model.")
    spoiled = shadow_autonomy_report(session, thresholds=thresholds)

    assert spoiled.meets_thresholds is False
    assert any("false positive" in blocker for blocker in spoiled.blockers)
    assert "explicit" in spoiled.note.lower()  # meeting thresholds never enables it by itself
