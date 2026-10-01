"""Experiment memory core: hypothesis → genome → experiment.

Rows here are evidence. Hypotheses and genomes are never edited after creation;
an experiment changes only by advancing its lifecycle states and by gaining
append-only children (attempts, assets, QA results, publications, ledger rows).
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, JSONDict, enum_column
from app.experiments.states import ExperimentConclusion, ExperimentStatus, VideoStatus
from app.ips.models import IP

if TYPE_CHECKING:
    from app.budgets.models import BudgetLedgerEntry
    from app.production.models import Asset, GenerationAttempt
    from app.publishing.models import Publication
    from app.quality.models import HumanReview, QAResult


class Hypothesis(Identified, Base):
    """An explicit, falsifiable creative claim an experiment tests."""

    __tablename__ = "hypotheses"

    ip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ips.id"))
    statement: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    prediction: Mapped[JSONDict]
    """Machine-readable expectation, e.g. metric, direction, genes under test."""
    source: Mapped[str] = mapped_column(String(40))
    """Who/what proposed it: fixture, human, creative_agent, ..."""

    ip: Mapped[IP] = relationship()


class CreativeGenome(Identified, Base):
    """Structured genes plus the free-form creative specification."""

    __tablename__ = "creative_genomes"

    schema_version: Mapped[int]
    genes: Mapped[JSONDict]
    creative_spec: Mapped[str] = mapped_column(Text)


class Experiment(Identified, Base):
    """One candidate video testing one hypothesis with one genome."""

    __tablename__ = "experiments"

    ip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ips.id"), index=True)
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypotheses.id"))
    genome_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("creative_genomes.id"))
    lineage_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, index=True)
    """Shared by an experiment and all its descendants; fresh for novel ones."""
    generation_reason: Mapped[str] = mapped_column(Text)
    """Why this candidate exists, in words an operator can audit."""
    output_requirements: Mapped[JSONDict]
    """What the produced video must satisfy (see ``OutputRequirements``)."""
    status: Mapped[ExperimentStatus] = mapped_column(
        enum_column(ExperimentStatus), default=ExperimentStatus.HYPOTHESIS_CREATED
    )
    video_status: Mapped[VideoStatus] = mapped_column(
        enum_column(VideoStatus), default=VideoStatus.PROPOSED
    )
    conclusion: Mapped[ExperimentConclusion | None] = mapped_column(
        enum_column(ExperimentConclusion)
    )

    ip: Mapped[IP] = relationship()
    hypothesis: Mapped[Hypothesis] = relationship()
    genome: Mapped[CreativeGenome] = relationship()
    generation_attempts: Mapped[list["GenerationAttempt"]] = relationship(
        back_populates="experiment", order_by="GenerationAttempt.attempt_number"
    )
    assets: Mapped[list["Asset"]] = relationship(
        back_populates="experiment", order_by="Asset.created_at"
    )
    qa_results: Mapped[list["QAResult"]] = relationship(
        back_populates="experiment", order_by="QAResult.created_at"
    )
    human_reviews: Mapped[list["HumanReview"]] = relationship(
        back_populates="experiment", order_by="HumanReview.created_at"
    )
    publications: Mapped[list["Publication"]] = relationship(
        back_populates="experiment", order_by="Publication.created_at"
    )
    ledger_entries: Mapped[list["BudgetLedgerEntry"]] = relationship(
        back_populates="experiment", order_by="BudgetLedgerEntry.created_at"
    )
