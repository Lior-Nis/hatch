"""Evolution evidence: parentage, mutations, and selection decisions."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict, enum_column, utcnow
from app.experiments.models import Experiment
from app.ips.models import IP


class ParentRelation(StrEnum):
    EXPLOIT = "exploit"
    MUTATION = "mutation"
    RECOMBINATION = "recombination"
    REPLICATION = "replication"
    RESURRECTION = "resurrection"


class ExperimentParent(Evidence, Base):
    """Edge of the lineage graph: ``experiment`` descends from ``parent``."""

    __tablename__ = "experiment_parents"
    __table_args__ = (CheckConstraint("experiment_id <> parent_id", name="no_self_parent"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), primary_key=True)
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), primary_key=True)
    relation: Mapped[ParentRelation] = mapped_column(enum_column(ParentRelation))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    experiment: Mapped[Experiment] = relationship(
        back_populates="parents", foreign_keys=[experiment_id]
    )
    parent: Mapped[Experiment] = relationship(back_populates="children", foreign_keys=[parent_id])


class MutationOperation(StrEnum):
    MUTATE = "mutate"
    RECOMBINE = "recombine"


class Mutation(Evidence, Identified, Base):
    """One gene changed when creating a descendant."""

    __tablename__ = "mutations"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    operation: Mapped[MutationOperation] = mapped_column(enum_column(MutationOperation))
    gene: Mapped[str] = mapped_column(String(80))
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    source_experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiments.id"))
    """For recombination: the lineage the gene value was borrowed from."""
    rationale: Mapped[str] = mapped_column(Text)

    experiment: Mapped[Experiment] = relationship(
        back_populates="mutations", foreign_keys=[experiment_id]
    )
    source_experiment: Mapped[Experiment | None] = relationship(foreign_keys=[source_experiment_id])


class DecisionType(StrEnum):
    CREATE_EXPERIMENT = "create_experiment"
    REQUEST_REPLICATION = "request_replication"
    CONCLUDE_EXPERIMENT = "conclude_experiment"
    PROMOTE_IP = "promote_ip"
    ARCHIVE_IP = "archive_ip"
    RESURRECT_IP = "resurrect_ip"


class AllocationBucket(StrEnum):
    EXPLOIT = "exploit"
    MUTATE = "mutate"
    EXPLORE = "explore"


class SelectionDecision(Evidence, Identified, Base):
    """Why the system selected, mutated, promoted, archived, or killed
    something — with the evidence it used, in machine-readable form."""

    __tablename__ = "selection_decisions"
    # Set once, when the candidate a decision asked for has been created.
    __mutable_columns__ = frozenset({"resulting_experiment_id"})

    decision_type: Mapped[DecisionType] = mapped_column(enum_column(DecisionType))
    bucket: Mapped[AllocationBucket | None] = mapped_column(enum_column(AllocationBucket))
    ip_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ips.id"), index=True)
    subject_experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiments.id"))
    """The experiment the decision is about (e.g. the selected parent)."""
    resulting_experiment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("experiments.id"))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[JSONDict]
    policy_version: Mapped[str] = mapped_column(String(60))

    ip: Mapped[IP | None] = relationship()
    subject_experiment: Mapped[Experiment | None] = relationship(
        foreign_keys=[subject_experiment_id]
    )
    resulting_experiment: Mapped[Experiment | None] = relationship(
        foreign_keys=[resulting_experiment_id]
    )
