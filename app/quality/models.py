import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, JSONDict, enum_column
from app.experiments.models import Experiment
from app.production.models import Asset
from app.quality.ports import QAOutcome


class QAResult(Identified, Base):
    """One gate's verdict on one asset. Append-only."""

    __tablename__ = "qa_results"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    gate: Mapped[str] = mapped_column(String(60))
    gate_version: Mapped[str] = mapped_column(String(60))
    mandatory: Mapped[bool]
    outcome: Mapped[QAOutcome] = mapped_column(enum_column(QAOutcome))
    scores: Mapped[JSONDict]
    reasons: Mapped[list[str]]
    details: Mapped[JSONDict] = mapped_column(default=dict)

    experiment: Mapped[Experiment] = relationship(back_populates="qa_results")
    asset: Mapped[Asset] = relationship()
