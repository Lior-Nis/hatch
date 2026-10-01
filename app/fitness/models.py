import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.analytics.models import MetricSnapshot
from app.db import Base, Evidence, Identified, JSONDict, enum_column
from app.experiments.models import Experiment
from app.ips.models import IP
from app.platforms import Platform
from app.publishing.models import Publication


class FitnessScope(StrEnum):
    VIDEO = "video"
    IP = "ip"


class FitnessSnapshot(Evidence, Identified, Base):
    """A fitness value at a point in time, with the inputs and formula version
    that produced it. VideoFitness and IPFitness are distinct scopes: a video
    score is per experiment (and per platform); an IP score aggregates."""

    __tablename__ = "fitness_snapshots"
    __table_args__ = (
        CheckConstraint(
            "scope <> 'video' OR experiment_id IS NOT NULL", name="video_fitness_has_experiment"
        ),
    )

    scope: Mapped[FitnessScope] = mapped_column(enum_column(FitnessScope))
    ip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ips.id"), index=True)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    publication_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("publications.id"))
    metric_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("metric_snapshots.id"))
    platform: Mapped[Platform | None] = mapped_column(enum_column(Platform))
    checkpoint: Mapped[str | None] = mapped_column(String(20))
    evaluator: Mapped[str] = mapped_column(String(60))
    evaluator_version: Mapped[str] = mapped_column(String(60))
    score: Mapped[float]
    components: Mapped[JSONDict]
    inputs: Mapped[JSONDict]

    ip: Mapped[IP] = relationship()
    experiment: Mapped[Experiment | None] = relationship(back_populates="fitness_snapshots")
    publication: Mapped[Publication | None] = relationship()
    metric_snapshot: Mapped[MetricSnapshot | None] = relationship()
