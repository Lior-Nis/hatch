"""Fake publisher: in-memory, idempotent on ``publication_id``."""

from datetime import UTC, datetime

from app.platforms import Platform
from app.publishing.ports import (
    PublicationState,
    PublicationStatus,
    PublisherChannel,
    PublishReceipt,
    PublishRequest,
    PublishTargetError,
)


class FakePublisher:
    provider = "fake"

    def __init__(self, *, accounts: set[str]) -> None:
        self._accounts = accounts
        self._external_by_publication: dict[str, str] = {}
        self._status: dict[str, PublicationStatus] = {}
        self.requests: dict[str, PublishRequest] = {}

    @property
    def post_count(self) -> int:
        return len(self._status)

    def schedule(self, request: PublishRequest) -> PublishReceipt:
        return self._create(request, PublicationStatus(state=PublicationState.SCHEDULED))

    def publish(self, request: PublishRequest) -> PublishReceipt:
        post_id = f"{request.platform.value}-post-{request.publication_id}"
        status = PublicationStatus(
            state=PublicationState.PUBLISHED,
            platform_post_id=post_id,
            permalink=f"https://fake.example/{request.platform.value}/{post_id}",
            published_at=datetime.now(UTC),
        )
        return self._create(request, status)

    def deliver(self, external_id: str) -> None:
        """Test hook: the platform has published a scheduled post."""
        request = self.requests[external_id]
        post_id = f"{request.platform.value}-post-{request.publication_id}"
        self._status[external_id] = PublicationStatus(
            state=PublicationState.PUBLISHED,
            platform_post_id=post_id,
            permalink=f"https://fake.example/{request.platform.value}/{post_id}",
            published_at=datetime.now(UTC),
        )

    def fail(self, external_id: str, error: str) -> None:
        """Test hook: the platform rejected a scheduled post."""
        self._status[external_id] = PublicationStatus(state=PublicationState.FAILED, error=error)

    def list_channels(self) -> list[PublisherChannel]:
        return [
            PublisherChannel(id=account, platform=Platform.YOUTUBE_SHORTS, name=account)
            for account in sorted(self._accounts)
        ]

    def cancel(self, external_id: str) -> None:
        self._status[external_id] = PublicationStatus(state=PublicationState.CANCELLED)

    def get_status(self, external_id: str) -> PublicationStatus:
        return self._status[external_id]

    def _create(self, request: PublishRequest, status: PublicationStatus) -> PublishReceipt:
        if request.platform_account_id not in self._accounts:
            raise PublishTargetError(f"account not connected: {request.platform_account_id}")
        external_id = self._external_by_publication.get(request.publication_id)
        if external_id is None:
            external_id = f"fake-post-{len(self._status) + 1}"
            self._external_by_publication[request.publication_id] = external_id
            self._status[external_id] = status
            self.requests[external_id] = request
        return PublishReceipt(provider=self.provider, external_id=external_id)
