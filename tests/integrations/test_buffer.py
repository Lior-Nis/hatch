"""Buffer adapter tests against a stub of Buffer's GraphQL API, built from the
request shapes in docs/research/buffer-publishing.md. No network."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.platforms import Platform
from app.publishing.ports import (
    PublicationState,
    Publisher,
    PublisherError,
    PublishRequest,
    PublishTargetError,
)
from integrations.buffer.publisher import BufferPublisher, parse_platform_post_id

WHEN = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class StubBuffer:
    """Just enough of Buffer's GraphQL API to exercise the adapter."""

    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.drop_next_create_response = False
        self.scheduling_type = "automatic"
        self.mutation_error: str | None = None
        self.channels = [
            {"id": "ch-yt", "name": "Nibbin Hollow", "service": "youtube", "serviceId": "UC1",
             "isDisconnected": False, "isLocked": False, "metadata": {"defaultToReminders": False}},
            {"id": "ch-tt", "name": "nibbinhollow", "service": "tiktok", "serviceId": "tt1",
             "isDisconnected": False, "isLocked": False, "metadata": {"defaultToReminders": True}},
            {"id": "ch-x", "name": "x", "service": "twitter", "serviceId": "x1",
             "isDisconnected": False, "isLocked": False, "metadata": {}},
        ]  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer key-123"
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables", {})
        if "createPost" in query:
            self.calls.append(("createPost", variables))
            if self.mutation_error:
                return self._data({"createPost": {"message": self.mutation_error}})
            post_id = f"post-{len(self.posts) + 1}"
            data = variables["input"]
            self.posts[post_id] = {
                "id": post_id,
                "status": "sending" if data["mode"] == "shareNow" else "scheduled",
                "schedulingType": self.scheduling_type,
                "dueAt": data.get("dueAt"),
                "text": data["text"],
                "channelId": data["channelId"],
                "createdAt": NOW.isoformat(),
                "sentAt": None,
                "externalLink": None,
                "error": None,
            }
            if self.drop_next_create_response:
                self.drop_next_create_response = False
                raise httpx.ReadTimeout("response lost")
            return self._data({"createPost": {"post": self.posts[post_id]}})
        if "deletePost" in query:
            self.calls.append(("deletePost", variables))
            self.posts.pop(variables["input"]["id"], None)
            return self._data({"deletePost": {"id": variables["input"]["id"]}})
        if "posts(" in query:
            self.calls.append(("posts", variables))
            channel = variables["input"]["filter"]["channelIds"][0]
            edges = [{"node": p} for p in self.posts.values() if p["channelId"] == channel]
            return self._data({"posts": {"edges": edges}})
        if "post(" in query:
            self.calls.append(("post", variables))
            return self._data({"post": self.posts.get(variables["input"]["id"])})
        if "channels(" in query:
            return self._data({"channels": self.channels})
        if "organizations" in query:
            return self._data({"account": {"organizations": [{"id": "org-1", "name": "Hatch"}]}})
        raise AssertionError(f"unexpected query: {query}")

    @staticmethod
    def _data(data: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"data": data})

    def created(self) -> list[dict[str, Any]]:
        return [variables["input"] for name, variables in self.calls if name == "createPost"]


@pytest.fixture
def buffer() -> StubBuffer:
    return StubBuffer()


@pytest.fixture
def publisher(buffer: StubBuffer) -> BufferPublisher:
    return BufferPublisher(
        api_key="key-123",
        client=httpx.Client(transport=httpx.MockTransport(buffer)),
        clock=lambda: NOW,
    )


def request(**overrides: Any) -> PublishRequest:
    fields: dict[str, Any] = {
        "publication_id": "pub-1",
        "platform": Platform.YOUTUBE_SHORTS,
        "platform_account_id": "ch-yt",
        "media_url": "https://media.example.com/experiments/e/a.mp4",
        "title": "Where the glow comes from | Nibbin Hollow",
        "text": "A gentle story.\n\n#NibbinHollow #Shorts",
        "thumbnail_offset_ms": 2000,
        "made_for_kids": True,
        "ai_generated": True,
        "scheduled_at": WHEN,
    }
    fields.update(overrides)
    return PublishRequest.model_validate(fields)


def test_adapter_satisfies_the_publisher_contract(publisher: BufferPublisher) -> None:
    contract: Publisher = publisher
    assert contract.provider == "buffer"


def test_schedule_creates_an_automatic_post_with_youtube_metadata(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    receipt = publisher.schedule(request())

    assert receipt.external_id == "post-1"
    [sent] = buffer.created()
    assert sent["channelId"] == "ch-yt"
    assert sent["mode"] == "customScheduled"
    assert sent["schedulingType"] == "automatic"
    assert sent["dueAt"] == "2026-10-02T15:00:00.000Z"
    assert sent["text"].startswith("A gentle story.")
    assert sent["assets"] == [
        {"video": {"url": "https://media.example.com/experiments/e/a.mp4",
                   "metadata": {"thumbnailOffset": 2000}}}
    ]  # fmt: skip
    assert sent["metadata"] == {
        "youtube": {
            "title": "Where the glow comes from | Nibbin Hollow",
            "categoryId": "24",
            "madeForKids": True,
            "privacy": "public",
            "isAiGenerated": True,
        }
    }


@pytest.mark.parametrize(
    ("platform", "metadata"),
    [
        (Platform.TIKTOK, {"tiktok": {"isAiGenerated": True}}),
        (
            Platform.INSTAGRAM_REELS,
            {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": True}},
        ),
        (Platform.FACEBOOK_REELS, {"facebook": {"type": "reel"}}),
    ],
)
def test_each_platform_gets_its_own_metadata_shape(
    publisher: BufferPublisher, buffer: StubBuffer, platform: Platform, metadata: dict[str, Any]
) -> None:
    publisher.schedule(request(platform=platform, title=None, made_for_kids=None))

    assert buffer.created()[0]["metadata"] == metadata


def test_publish_now_uses_share_now_without_a_due_date(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    publisher.publish(request(scheduled_at=None))

    [sent] = buffer.created()
    assert sent["mode"] == "shareNow"
    assert "dueAt" not in sent


def test_scheduling_the_same_publication_twice_creates_one_post(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    first = publisher.schedule(request())
    second = publisher.schedule(request())

    assert second.external_id == first.external_id
    assert len(buffer.posts) == 1


def test_a_lost_response_does_not_lead_to_a_duplicate_post_on_retry(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    buffer.drop_next_create_response = True
    with pytest.raises(PublisherError) as lost:
        publisher.schedule(request())
    assert lost.value.retryable

    receipt = publisher.schedule(request())  # the retry looks before it creates

    assert len(buffer.posts) == 1
    assert receipt.external_id == "post-1"
    assert len(buffer.created()) == 1


def test_different_videos_on_the_same_channel_are_not_confused(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    publisher.schedule(request())
    publisher.schedule(
        request(
            publication_id="pub-2", text="Another story.", scheduled_at=WHEN + timedelta(hours=8)
        )
    )

    assert len(buffer.posts) == 2


def test_a_post_that_would_only_be_a_phone_reminder_is_removed_and_refused(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    buffer.scheduling_type = "notification"

    with pytest.raises(PublishTargetError, match="reminder"):
        publisher.schedule(request())

    assert buffer.posts == {}


def test_a_mutation_error_is_a_definitive_refusal(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    buffer.mutation_error = "Could not fetch media from the provided URL"

    with pytest.raises(PublishTargetError, match="Could not fetch media"):
        publisher.schedule(request())


def test_status_of_a_scheduled_then_sent_post(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    receipt = publisher.schedule(request())
    assert publisher.get_status(receipt.external_id).state is PublicationState.SCHEDULED

    buffer.posts["post-1"].update(
        status="sent",
        sentAt="2026-10-02T15:00:07.000Z",
        externalLink="https://www.youtube.com/shorts/dQw4w9WgXcQ",
    )
    status = publisher.get_status(receipt.external_id)

    assert status.state is PublicationState.PUBLISHED
    assert status.permalink == "https://www.youtube.com/shorts/dQw4w9WgXcQ"
    assert status.platform_post_id == "dQw4w9WgXcQ"
    assert status.published_at == datetime(2026, 10, 2, 15, 0, 7, tzinfo=UTC)


def test_status_of_a_failed_and_of_a_deleted_post(
    publisher: BufferPublisher, buffer: StubBuffer
) -> None:
    receipt = publisher.schedule(request())
    buffer.posts["post-1"].update(status="error", error={"message": "Video too short"})

    failed = publisher.get_status(receipt.external_id)
    publisher.cancel(receipt.external_id)
    gone = publisher.get_status(receipt.external_id)

    assert (failed.state, failed.error) == (PublicationState.FAILED, "Video too short")
    assert gone.state is PublicationState.CANCELLED


def test_channels_are_listed_with_platform_and_automatic_flag(publisher: BufferPublisher) -> None:
    channels = {channel.id: channel for channel in publisher.list_channels()}

    assert set(channels) == {"ch-yt", "ch-tt"}  # unsupported networks are left out
    assert channels["ch-yt"].platform is Platform.YOUTUBE_SHORTS
    assert channels["ch-yt"].external_account_id == "UC1"
    assert channels["ch-yt"].automatic is True
    assert channels["ch-tt"].automatic is False  # notifications by default are on


def test_http_and_graphql_errors_are_reported_without_leaking_the_key(buffer: StubBuffer) -> None:
    def unauthorized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errors": [{"message": "bad token"}]})

    def graphql_error(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "RATE_LIMIT_EXCEEDED"}]})

    for handler in (unauthorized, graphql_error):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(PublisherError) as error:
            BufferPublisher(api_key="key-123", client=client).get_status("post-1")
        assert "key-123" not in str(error.value)


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        (Platform.YOUTUBE_SHORTS, "https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        (Platform.YOUTUBE_SHORTS, "https://youtube.com/watch?v=dQw4w9WgXcQ&t=1", "dQw4w9WgXcQ"),
        (
            Platform.TIKTOK,
            "https://www.tiktok.com/@nib/video/7412345678901234567",
            "7412345678901234567",
        ),
        (Platform.FACEBOOK_REELS, "https://www.facebook.com/reel/1234567890", "1234567890"),
        (Platform.INSTAGRAM_REELS, "https://www.instagram.com/reel/C8abcDEfgh1/", "C8abcDEfgh1"),
        (Platform.TIKTOK, "https://example.com/nothing", None),
    ],
)
def test_native_post_ids_are_parsed_from_permalinks(
    platform: Platform, url: str, expected: str | None
) -> None:
    assert parse_platform_post_id(platform, url) == expected
