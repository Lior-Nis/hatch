import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, Identified, JSONDict, enum_column
from app.experiments.models import Experiment
from app.ips.models import IP
from app.platforms import Platform
from app.production.models import Asset

if TYPE_CHECKING:
    from app.analytics.models import MetricSnapshot


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISCONNECTED = "disconnected"


class PlatformAccount(Identified, Base):
    """A human-owned social identity: one brand account per IP per platform.
    Holds identifiers only — never credentials."""

    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint("ip_id", "platform"),
        UniqueConstraint("platform", "external_account_id"),
    )

    ip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ips.id"), index=True)
    platform: Mapped[Platform] = mapped_column(enum_column(Platform))
    external_account_id: Mapped[str] = mapped_column(String(200))
    """The platform's own id for the account (channel id, page id, ...)."""
    publisher_profile_id: Mapped[str | None] = mapped_column(String(200))
    """The publishing vendor's id for this connection (e.g. Buffer channel)."""
    handle: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[AccountStatus] = mapped_column(
        enum_column(AccountStatus), default=AccountStatus.ACTIVE
    )

    ip: Mapped[IP] = relationship()


class PublicationRecordStatus(StrEnum):
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Publication(Identified, Base):
    """Hatch's own record of one platform package of a canonical video."""

    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("experiment_id", "platform"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id"))
    platform: Mapped[Platform] = mapped_column(enum_column(Platform))
    status: Mapped[PublicationRecordStatus] = mapped_column(
        enum_column(PublicationRecordStatus), default=PublicationRecordStatus.PLANNED
    )
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("platform_accounts.id")
    )
    package: Mapped[JSONDict] = mapped_column(default=dict)
    """The platform packaging (``PlatformPackage``): title, caption, hashtags,
    call to action, thumbnail frame, policy flags."""
    publisher: Mapped[str | None] = mapped_column(String(60))
    external_id: Mapped[str | None] = mapped_column(String(200))
    """The publisher's (e.g. Buffer's) id for the post."""
    permalink: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    platform_post_id: Mapped[str | None] = mapped_column(String(200))
    scheduled_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]

    experiment: Mapped[Experiment] = relationship(back_populates="publications")
    asset: Mapped[Asset] = relationship()
    platform_account: Mapped[PlatformAccount | None] = relationship()
    metric_snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="publication", order_by="MetricSnapshot.observed_at"
    )
