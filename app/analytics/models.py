import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Evidence, Identified, JSONDict
from app.publishing.models import Publication


class MetricSnapshot(Evidence, Identified, Base):
    """One timestamped observation of one publication's metrics. The raw
    platform payload and Hatch's normalized view are stored side by side and
    never rewritten; later observations are new rows."""

    __tablename__ = "metric_snapshots"

    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("publications.id"), index=True)
    observed_at: Mapped[datetime]
    hours_since_publication: Mapped[float]
    checkpoint: Mapped[str | None] = mapped_column(String(20))
    """Maturity window this observation was scheduled for: 1h, 6h, 24h, 72h, 7d, 30d."""
    adapter: Mapped[str] = mapped_column(String(60))
    adapter_version: Mapped[str] = mapped_column(String(60))
    raw: Mapped[JSONDict]
    normalized: Mapped[JSONDict]

    publication: Mapped[Publication] = relationship(back_populates="metric_snapshots")
