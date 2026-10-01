"""Publishing port.

``Publisher`` abstracts the distribution vendor (Buffer in V1). The vendor is
never the source of truth: Hatch owns publication identity and schedule intent,
and stores the returned external ids against its own ``publication_id``.
"""

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.platforms import Platform


class PublishTargetError(Exception):
    """The target account is not connected to / permitted for this publisher."""


class PublicationState(StrEnum):
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishRequest(BaseModel):
    """One platform package of a canonical video."""

    model_config = ConfigDict(frozen=True)

    publication_id: str = Field(min_length=1)
    """Hatch's own id; also the idempotency key for the external side effect."""
    platform: Platform
    platform_account_id: str
    media_url: str
    title: str
    caption: str
    hashtags: tuple[str, ...] = ()
    thumbnail_url: str | None = None
    scheduled_at: datetime | None = None


class PublishReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    external_id: str
    """The publisher's id for the post (not the platform's post id)."""


class PublicationStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: PublicationState
    platform_post_id: str | None = None
    permalink: str | None = None
    published_at: datetime | None = None
    error: str | None = None


class Publisher(Protocol):
    @property
    def provider(self) -> str: ...

    def schedule(self, request: PublishRequest) -> PublishReceipt:
        """Queue a post for ``request.scheduled_at``. Idempotent on
        ``publication_id``."""
        ...

    def publish(self, request: PublishRequest) -> PublishReceipt:
        """Publish as soon as possible. Idempotent on ``publication_id``."""
        ...

    def cancel(self, external_id: str) -> None:
        """Cancel a post that has not been published yet."""
        ...

    def get_status(self, external_id: str) -> PublicationStatus: ...
