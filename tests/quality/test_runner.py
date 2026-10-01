from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import QAResult, ReviewDecision
from app.quality.ports import QACandidate, QAOutcome, QAVerdict
from app.quality.review import ReviewNotAllowed, submit_review
from app.quality.runner import QANotAllowed, run_quality_gates
from app.quality.technical import TechnicalQAGate
from integrations.fake.quality import FakeQAGate
from integrations.object_storage.local import LocalAssetStore
from tests.factories import final_asset, make_experiment, make_generated_experiment


class ExplodingGate:
    name = "visual"
    version = "boom-1"
    mandatory = True

    def evaluate(self, candidate: QACandidate) -> QAVerdict:
        raise RuntimeError("model unavailable")


@pytest.fixture
def store(tmp_path: Path) -> LocalAssetStore:
    return LocalAssetStore(tmp_path / "assets")


@pytest.fixture
def experiment(session: Session, tmp_path: Path, store: LocalAssetStore) -> Experiment:
    return make_generated_experiment(session, tmp_path, store=store)


def status(session: Session, experiment: Experiment) -> VideoStatus:
    session.expire_all()
    return session.get_one(Experiment, experiment.id).video_status


def test_passing_gates_send_the_video_to_human_approval(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    report = run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=store)

    assert report.rejected is False
    assert status(session, experiment) is VideoStatus.APPROVAL_PENDING
    [result] = session.scalars(select(QAResult)).all()
    assert (result.gate, result.gate_version, result.mandatory) == ("technical", "1", True)
    assert result.outcome is QAOutcome.PASS
    assert result.asset_id == final_asset(experiment).id
    assert result.details["media"]["height"] == 1920


def test_mandatory_failure_rejects_the_video_and_records_the_reasons(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    safety = FakeQAGate(
        name="child_safety", mandatory=True, outcome=QAOutcome.FAIL, reasons=["frightening imagery"]
    )

    report = run_quality_gates(
        session, experiment.id, gates=[TechnicalQAGate(), safety], store=store
    )

    assert report.rejected is True
    assert status(session, experiment) is VideoStatus.QA_REJECTED
    results = {r.gate: r for r in session.scalars(select(QAResult)).all()}
    assert results["technical"].outcome is QAOutcome.PASS
    assert results["child_safety"].outcome is QAOutcome.FAIL
    assert results["child_safety"].reasons == ["frightening imagery"]


def test_a_mandatory_rejection_cannot_be_overridden_by_human_approval(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    safety = FakeQAGate(name="child_safety", mandatory=True, outcome=QAOutcome.FAIL)
    run_quality_gates(session, experiment.id, gates=[safety], store=store)

    with pytest.raises(ReviewNotAllowed):
        submit_review(
            session,
            experiment.id,
            decision=ReviewDecision.APPROVE,
            reason="It will perform really well.",
            reviewer="lior",
        )

    assert status(session, experiment) is VideoStatus.QA_REJECTED


def test_advisory_failure_is_recorded_but_does_not_reject(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    pacing = FakeQAGate(name="creative", mandatory=False, outcome=QAOutcome.FAIL, reasons=["slow"])

    report = run_quality_gates(session, experiment.id, gates=[pacing], store=store)

    assert report.rejected is False
    assert status(session, experiment) is VideoStatus.APPROVAL_PENDING
    assert session.scalars(select(QAResult)).one().outcome is QAOutcome.FAIL


def test_escalation_goes_to_a_human_and_is_flagged(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    unsure = FakeQAGate(name="child_safety", mandatory=True, outcome=QAOutcome.ESCALATE)

    report = run_quality_gates(session, experiment.id, gates=[unsure], store=store)

    assert report.escalated is True
    assert report.rejected is False
    assert status(session, experiment) is VideoStatus.APPROVAL_PENDING


def test_a_crashing_gate_never_counts_as_a_pass(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    report = run_quality_gates(session, experiment.id, gates=[ExplodingGate()], store=store)

    assert report.escalated is True
    result = session.scalars(select(QAResult)).one()
    assert result.outcome is QAOutcome.ESCALATE
    assert "model unavailable" in result.reasons[0]


def test_running_with_no_gates_is_refused(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    with pytest.raises(ValueError, match="no QA gates"):
        run_quality_gates(session, experiment.id, gates=[], store=store)

    assert status(session, experiment) is VideoStatus.GENERATED


def test_a_video_that_was_not_generated_cannot_be_checked(
    session: Session, store: LocalAssetStore
) -> None:
    experiment = make_experiment(session)

    with pytest.raises(QANotAllowed):
        run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=store)


def test_rerunning_qa_after_a_decision_does_not_duplicate_results(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=store)

    again = run_quality_gates(session, experiment.id, gates=[TechnicalQAGate()], store=store)

    assert again.video_status is VideoStatus.APPROVAL_PENDING
    assert len(session.scalars(select(QAResult)).all()) == 1


def test_gates_after_a_mandatory_failure_are_not_run(
    session: Session, experiment: Experiment, store: LocalAssetStore
) -> None:
    broken = FakeQAGate(name="technical", mandatory=True, outcome=QAOutcome.FAIL)
    paid_review = ExplodingGate()  # would raise if it were evaluated

    report = run_quality_gates(session, experiment.id, gates=[broken, paid_review], store=store)

    assert report.rejected is True
    assert [v.gate for v in report.verdicts] == ["technical"]
    assert len(session.scalars(select(QAResult)).all()) == 1
