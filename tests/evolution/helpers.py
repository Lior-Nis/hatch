"""Builders for evolution tests: experiments with fitness evidence."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.evolution.models import ExperimentParent, ParentRelation
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.platforms import Platform
from tests.factories import make_experiment

T0 = datetime(2026, 10, 1, tzinfo=UTC)
_sequence = 0


def evaluated(
    session: Session,
    score: float | None,
    *,
    parent: Experiment | None = None,
    relation: ParentRelation = ParentRelation.REPLICATION,
    ip_slug: str | None = None,
    confidence: float = 1.0,
    platforms: tuple[Platform, ...] = (Platform.YOUTUBE_SHORTS,),
    status: ExperimentStatus = ExperimentStatus.EARLY_EVALUATED,
    video_status: VideoStatus = VideoStatus.OBSERVING,
) -> Experiment:
    """An experiment that has been published and (unless ``score`` is None)
    has early fitness evidence."""
    global _sequence
    _sequence += 1
    experiment = make_experiment(
        session, ip_slug=ip_slug, lineage_id=parent.lineage_id if parent else None
    )
    session.execute(
        update(Experiment)
        .where(Experiment.id == experiment.id)
        .values(
            status=status,
            video_status=video_status,
            created_at=T0 + timedelta(minutes=_sequence),
        )
    )
    session.expire(experiment)
    if parent is not None:
        session.add(ExperimentParent(experiment=experiment, parent=parent, relation=relation))
    if score is not None:
        for platform in platforms:
            session.add(
                FitnessSnapshot(
                    scope=FitnessScope.VIDEO,
                    ip_id=experiment.ip_id,
                    experiment=experiment,
                    platform=platform,
                    checkpoint="72h",
                    evaluator="heuristic",
                    evaluator_version="1",
                    score=score,
                    components={},
                    inputs={"confidence": confidence},
                )
            )
    session.flush()
    return experiment
