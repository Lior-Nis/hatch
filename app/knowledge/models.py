"""Derived knowledge: the current interpretation of the evidence.

Unlike experiment evidence, summaries are editable — they are revised as new
experiments arrive. Each one must cite the experiments that support it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, ForeignKey, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, utcnow
from app.experiments.models import Experiment
from app.ips.models import IP

knowledge_summary_experiments = Table(
    "knowledge_summary_experiments",
    Base.metadata,
    Column("summary_id", ForeignKey("knowledge_summaries.id"), primary_key=True),
    Column("experiment_id", ForeignKey("experiments.id"), primary_key=True),
)


class KnowledgeSummary(Identified, Base):
    __tablename__ = "knowledge_summaries"

    ip_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ips.id"), index=True)
    """Null for knowledge that applies across the whole portfolio."""
    topic: Mapped[str] = mapped_column(String(120))
    statement: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float]
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    ip: Mapped[IP | None] = relationship()
    supporting_experiments: Mapped[list[Experiment]] = relationship(
        secondary=knowledge_summary_experiments
    )
