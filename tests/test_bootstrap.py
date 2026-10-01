from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bootstrap import build_qa_gates
from app.config import Settings
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import QAResult
from app.quality.ports import QAOutcome
from app.quality.runner import run_quality_gates
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_generated_experiment


def test_without_a_language_model_content_gates_escalate_instead_of_passing(
    session: Session, tmp_path: Path
) -> None:
    store = LocalAssetStore(tmp_path / "assets")
    experiment = make_generated_experiment(session, tmp_path, store=store)
    gates = build_qa_gates(Settings(_env_file=None, anthropic_api_key=None))(session)

    report = run_quality_gates(session, experiment.id, gates=gates, store=store)

    assert [gate.name for gate in gates] == [
        "technical", "child_safety", "visual", "ip_brand", "creative",
    ]  # fmt: skip
    assert report.escalated is True
    assert report.rejected is False
    outcomes = {r.gate: r for r in session.scalars(select(QAResult))}
    assert outcomes["technical"].outcome is QAOutcome.PASS
    assert outcomes["child_safety"].outcome is QAOutcome.ESCALATE
    assert "no language model is configured" in outcomes["child_safety"].reasons[0]
    session.expire_all()
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING
