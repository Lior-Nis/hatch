from datetime import UTC, datetime, timedelta

import pytest

from app.platforms import Platform
from app.publishing.ports import (
    PublicationState,
    Publisher,
    PublishRequest,
    PublishTargetError,
)
from integrations.fake.publisher import FakePublisher


def _request(publication_id: str = "pub-1", **overrides: object) -> PublishRequest:
    fields: dict[str, object] = {
        "publication_id": publication_id,
        "platform": Platform.YOUTUBE_SHORTS,
        "platform_account_id": "yt-channel-1",
        "media_url": "https://assets.example/short.mp4",
        "title": "Snail paints a rainbow",
        "text": "Colours everywhere! #kids #colours",
    }
    fields.update(overrides)
    return PublishRequest.model_validate(fields)


def test_fake_publisher_satisfies_the_publisher_contract() -> None:
    publisher: Publisher = FakePublisher(accounts={"yt-channel-1"})
    assert publisher.provider == "fake"


def test_scheduling_returns_an_external_id_and_a_scheduled_status() -> None:
    publisher = FakePublisher(accounts={"yt-channel-1"})
    when = datetime.now(UTC) + timedelta(hours=2)

    receipt = publisher.schedule(_request(scheduled_at=when))
    status = publisher.get_status(receipt.external_id)

    assert status.state is PublicationState.SCHEDULED
    assert status.platform_post_id is None


def test_immediate_publish_yields_a_platform_post_id() -> None:
    publisher = FakePublisher(accounts={"yt-channel-1"})

    receipt = publisher.publish(_request())
    status = publisher.get_status(receipt.external_id)

    assert status.state is PublicationState.PUBLISHED
    assert status.platform_post_id is not None
    assert status.published_at is not None


def test_scheduling_the_same_publication_twice_creates_one_external_post() -> None:
    publisher = FakePublisher(accounts={"yt-channel-1"})
    when = datetime.now(UTC) + timedelta(hours=2)

    first = publisher.schedule(_request("pub-1", scheduled_at=when))
    second = publisher.schedule(_request("pub-1", scheduled_at=when))

    assert second.external_id == first.external_id
    assert publisher.post_count == 1


def test_cancelling_a_scheduled_post_marks_it_cancelled() -> None:
    publisher = FakePublisher(accounts={"yt-channel-1"})
    receipt = publisher.schedule(_request(scheduled_at=datetime.now(UTC) + timedelta(hours=2)))

    publisher.cancel(receipt.external_id)

    assert publisher.get_status(receipt.external_id).state is PublicationState.CANCELLED


def test_publishing_to_an_unconnected_account_is_rejected() -> None:
    publisher = FakePublisher(accounts={"yt-channel-1"})

    with pytest.raises(PublishTargetError):
        publisher.publish(_request(platform_account_id="someone-elses-channel"))
