"""Experiment memory core: hypothesis → genome → experiment.

Rows here are evidence. Hypotheses and genomes are never edited after creation;
an experiment changes only by advancing its lifecycle states and by gaining
append-only children (attempts, assets, QA results, publications, ledger rows).
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base, Evidence, Identified, JSONDict, enum_column, utcnow
from app.experiments.states import (
    EXPERIMENT_LIFECYCLE,
    VIDEO_LIFECYCLE,
    ExperimentConclusion,
    ExperimentStatus,
    VideoStatus,
)
from app.ips.models import IP

if TYPE_CHECKING:
    from app.budgets.models import BudgetLedgerEntry
    from app.characters.models import ExperimentCharacter
    from app.evolution.models import ExperimentParent, Mutation
    from app.fitness.models import FitnessSnapshot
    from app.production.models import Asset, GenerationAttempt
    from app.publishing.models import Publication
    from app.quality.models import HumanReview, QAResult
    from app.scheduling.models import JobRun


class Hypothesis(Evidence, Identified, Base):
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


class CreativeGenome(Evidence, Identified, Base):
    """Structured genes plus the free-form creative specification."""

    __tablename__ = "creative_genomes"
    __table_args__ = (Index("ix_creative_genomes_genes", "genes", postgresql_using="gin"),)

    schema_version: Mapped[int]
    genes: Mapped[JSONDict]
    creative_spec: Mapped[str] = mapped_column(Text)


class Experiment(Evidence, Identified, Base):
    """One candidate video testing one hypothesis with one genome."""

    __tablename__ = "experiments"
    __mutable_columns__ = frozenset(
        {"status", "video_status", "video_status_changed_at", "conclusion"}
    )

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
    video_status_changed_at: Mapped[datetime] = mapped_column(default=utcnow)
    """When the video last changed state; used to detect stalled pipelines."""
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
    parents: Mapped[list["ExperimentParent"]] = relationship(
        back_populates="experiment", foreign_keys="ExperimentParent.experiment_id"
    )
    children: Mapped[list["ExperimentParent"]] = relationship(
        back_populates="parent", foreign_keys="ExperimentParent.parent_id"
    )
    mutations: Mapped[list["Mutation"]] = relationship(
        back_populates="experiment",
        foreign_keys="Mutation.experiment_id",
        order_by="Mutation.created_at",
    )
    characters: Mapped[list["ExperimentCharacter"]] = relationship(back_populates="experiment")
    fitness_snapshots: Mapped[list["FitnessSnapshot"]] = relationship(
        back_populates="experiment", order_by="FitnessSnapshot.created_at"
    )
    job_runs: Mapped[list["JobRun"]] = relationship(
        back_populates="experiment", order_by="JobRun.created_at"
    )

    @validates("status")
    def _check_status(self, _key: str, target: ExperimentStatus) -> ExperimentStatus:
        if target is ExperimentStatus.CONCLUDED and self.conclusion is None:
            raise ValueError("an experiment needs a conclusion to be concluded; use conclude()")
        return EXPERIMENT_LIFECYCLE.validate_assignment(self.status, target)

    @validates("video_status")
    def _check_video_status(self, _key: str, target: VideoStatus) -> VideoStatus:
        validated = VIDEO_LIFECYCLE.validate_assignment(self.video_status, target)
        if self.video_status is not None and validated != self.video_status:
            self.video_status_changed_at = utcnow()
        return validated

    def conclude(self, conclusion: ExperimentConclusion) -> None:
        """Close the experiment with its verdict. A conclusion is final."""
        if self.conclusion is not None:
            raise ValueError(f"experiment {self.id} is already concluded")
        EXPERIMENT_LIFECYCLE.check(
            self.status or EXPERIMENT_LIFECYCLE.initial, ExperimentStatus.CONCLUDED
        )
        self.conclusion = conclusion
        self.status = ExperimentStatus.CONCLUDED
