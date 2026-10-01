import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, enum_column
from app.experiments.models import Experiment
from app.platforms import Platform
from app.production.models import Asset


class PublicationRecordStatus(StrEnum):
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Publication(Identified, Base):
    """Hatch's own record of one platform package of a canonical video.
    (Placeholder in the vertical slice: nothing is published yet.)"""

    __tablename__ = "publications"

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    platform: Mapped[Platform] = mapped_column(enum_column(Platform))
    status: Mapped[PublicationRecordStatus] = mapped_column(
        enum_column(PublicationRecordStatus), default=PublicationRecordStatus.PLANNED
    )
    platform_account_id: Mapped[str | None] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200))
    """The publisher's (e.g. Buffer's) id for the post."""
    platform_post_id: Mapped[str | None] = mapped_column(String(200))
    scheduled_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]

    experiment: Mapped[Experiment] = relationship(back_populates="publications")
    asset: Mapped[Asset] = relationship()
