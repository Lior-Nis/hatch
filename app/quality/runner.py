"""Run automated QA gates on a generated video and decide where it goes.

    GENERATED → QA_PENDING → APPROVAL_PENDING   (no mandatory gate failed)
                           → QA_REJECTED        (a mandatory gate failed)

Safety boundary: a mandatory FAIL is final for this asset. Nothing downstream —
human approval, fitness, predicted engagement, agent confidence — can move a
QA_REJECTED video forward. The runner is fail-closed: it refuses to run with no
gates, and a gate that crashes is recorded as ESCALATE, never as a pass.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.models import Asset, AssetKind
from app.quality.models import QAResult
from app.quality.ports import QACandidate, QAGate, QAOutcome, QAVerdict
from app.storage import AssetStore

logger = logging.getLogger(__name__)

_DECIDED = (VideoStatus.APPROVAL_PENDING, VideoStatus.QA_REJECTED)


class QANotAllowed(Exception):
    """The experiment has no generated video awaiting QA."""


@dataclass(frozen=True)
class QAReport:
    experiment_id: uuid.UUID
    video_status: VideoStatus
    verdicts: tuple[QAVerdict, ...]
    rejected: bool
    escalated: bool


def run_quality_gates(
    session: Session, experiment_id: uuid.UUID, *, gates: Sequence[QAGate], store: AssetStore
) -> QAReport:
    if not gates:
        raise ValueError("no QA gates configured: refusing to pass a video unchecked")

    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    if experiment.video_status in _DECIDED:
        return _existing_report(session, experiment)
    if experiment.video_status not in (VideoStatus.GENERATED, VideoStatus.QA_PENDING):
        raise QANotAllowed(
            f"experiment {experiment_id} has no generated video awaiting QA "
            f"(video is {experiment.video_status.value})"
        )
    asset = session.scalars(
        select(Asset)
        .where(Asset.experiment_id == experiment.id, Asset.kind == AssetKind.FINAL_VIDEO)
        .order_by(Asset.created_at.desc())
    ).first()
    if asset is None:
        raise QANotAllowed(f"experiment {experiment_id} has no final video asset")

    experiment.video_status = VideoStatus.QA_PENDING
    candidate = QACandidate(
        experiment_id=str(experiment.id),
        media_path=store.local_path(asset.storage_uri),
        genes=experiment.genome.genes,
        creative_spec=experiment.genome.creative_spec,
        hypothesis=experiment.hypothesis.statement,
        requirements=experiment.output_requirements,
    )

    verdicts: list[QAVerdict] = []
    rejected = escalated = False
    for gate in gates:
        verdict = _evaluate(gate, candidate)
        verdicts.append(verdict)
        session.add(
            QAResult(
                experiment=experiment,
                asset=asset,
                gate=verdict.gate,
                gate_version=verdict.gate_version,
                mandatory=gate.mandatory,
                outcome=verdict.outcome,
                scores=dict(verdict.scores),
                reasons=list(verdict.reasons),
                details=dict(verdict.details),
            )
        )
        if gate.mandatory and verdict.outcome is QAOutcome.FAIL:
            rejected = True
        if verdict.outcome is QAOutcome.ESCALATE:
            escalated = True

    experiment.video_status = VideoStatus.QA_REJECTED if rejected else VideoStatus.APPROVAL_PENDING
    session.commit()
    if rejected:
        logger.warning("qa_rejected", extra={"experiment_id": str(experiment.id)})
    return QAReport(
        experiment_id=experiment.id,
        video_status=experiment.video_status,
        verdicts=tuple(verdicts),
        rejected=rejected,
        escalated=escalated,
    )


def _evaluate(gate: QAGate, candidate: QACandidate) -> QAVerdict:
    try:
        return gate.evaluate(candidate)
    except Exception as exc:  # a broken gate must never read as a pass
        logger.exception("qa_gate_error", extra={"gate": gate.name})
        return QAVerdict(
            gate=gate.name,
            gate_version=gate.version,
            outcome=QAOutcome.ESCALATE,
            reasons=(f"gate error: {exc}",),
        )


def _existing_report(session: Session, experiment: Experiment) -> QAReport:
    results = session.scalars(
        select(QAResult)
        .where(QAResult.experiment_id == experiment.id)
        .order_by(QAResult.created_at)
    ).all()
    verdicts = tuple(
        QAVerdict(
            gate=result.gate,
            gate_version=result.gate_version,
            outcome=result.outcome,
            scores=result.scores,
            reasons=tuple(result.reasons),
            details=result.details,
        )
        for result in results
    )
    return QAReport(
        experiment_id=experiment.id,
        video_status=experiment.video_status,
        verdicts=verdicts,
        rejected=experiment.video_status is VideoStatus.QA_REJECTED,
        escalated=any(result.outcome is QAOutcome.ESCALATE for result in results),
    )
