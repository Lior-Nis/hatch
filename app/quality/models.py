import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict, enum_column
from app.experiments.models import Experiment
from app.production.models import Asset
from app.quality.ports import QAOutcome


class QAResult(Evidence, Identified, Base):
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


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class HumanReview(Evidence, Identified, Base):
    """A human reviewer's decision on one asset. Append-only: a decision is
    never edited, so reviews double as labelled QA data."""

    __tablename__ = "human_reviews"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    decision: Mapped[ReviewDecision] = mapped_column(enum_column(ReviewDecision))
    reason: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(120))

    experiment: Mapped[Experiment] = relationship(back_populates="human_reviews")
    asset: Mapped[Asset] = relationship()
