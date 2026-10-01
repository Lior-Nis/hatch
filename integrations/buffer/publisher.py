"""Buffer adapter for the ``Publisher`` port (GraphQL API, personal API key).

Buffer's vocabulary — channels, share modes, per-network metadata — stays in
this module. See docs/research/buffer-publishing.md for the API facts this is
built on.

Buffer has no idempotency key and no upload endpoint. Therefore:
- every create is preceded by a lookup for a post with the same channel, due
  time and text ("look before create"), so a retry after a lost response
  finds the post instead of publishing it twice;
- media is passed as a public URL that Buffer fetches when the post goes out.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.db import utcnow
from app.platforms import Platform
from app.publishing.ports import (
    PublicationState,
    PublicationStatus,
    PublisherChannel,
    PublisherError,
    PublishReceipt,
    PublishRequest,
    PublishTargetError,
)

API_URL = "https://api.buffer.com"
# YouTube category "Entertainment". Required by Buffer on create.
YOUTUBE_CATEGORY_ID = "24"

_PLATFORM_BY_SERVICE = {
    "youtube": Platform.YOUTUBE_SHORTS,
    "tiktok": Platform.TIKTOK,
    "instagram": Platform.INSTAGRAM_REELS,
    "facebook": Platform.FACEBOOK_REELS,
}
_STATE_BY_STATUS = {
    "draft": PublicationState.SCHEDULED,
    "needs_approval": PublicationState.SCHEDULED,
    "scheduled": PublicationState.SCHEDULED,
    "sending": PublicationState.SCHEDULED,
    "sent": PublicationState.PUBLISHED,
    "error": PublicationState.FAILED,
}

_CREATE = """
mutation Create($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id status schedulingType dueAt channelId } }
  ... on MutationError { message } } }"""
_DELETE = """
mutation Delete($input: DeletePostInput!) { deletePost(input: $input) {
  ... on DeletePostSuccess { id } ... on MutationError { message } } }"""
_POST = """
query Post($input: PostInput!) { post(input: $input) {
  id status schedulingType dueAt sentAt externalLink channelId channelService
  error { message } } }"""
_POSTS = """
query Posts($input: PostsInput!) { posts(first: 50, input: $input) {
  edges { node { id status dueAt text channelId createdAt } } } }"""
_CHANNELS = """
query Channels($input: ChannelsInput!) { channels(input: $input) {
  id name service serviceId isDisconnected isLocked
  metadata { ... on TiktokMetadata { defaultToReminders }
             ... on InstagramMetadata { defaultToReminders }
             ... on YoutubeMetadata { defaultToReminders } } } }"""
_ORGANIZATIONS = "query { account { organizations { id name } } }"

_POST_ID_PATTERNS = {
    Platform.YOUTUBE_SHORTS: re.compile(r"/shorts/([A-Za-z0-9_-]{6,})"),
    Platform.TIKTOK: re.compile(r"/video/(\d+)"),
    Platform.FACEBOOK_REELS: re.compile(r"/(?:reel|videos)/(\d+)"),
    # Instagram permalinks carry a shortcode; the numeric media id is resolved
    # through the Graph API by the analytics adapter.
    Platform.INSTAGRAM_REELS: re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)"),
}


def parse_platform_post_id(platform: Platform, permalink: str) -> str | None:
    """The platform's own id for a post, taken from its public URL."""
    match = _POST_ID_PATTERNS[platform].search(urlparse(permalink).path)
    if match:
        return match.group(1)
    if platform is Platform.YOUTUBE_SHORTS:
        watch_ids = parse_qs(urlparse(permalink).query).get("v")
        return watch_ids[0] if watch_ids else None
    return None


_PLATFORM_BY_HOST = {
    "youtube.com": Platform.YOUTUBE_SHORTS,
    "youtu.be": Platform.YOUTUBE_SHORTS,
    "tiktok.com": Platform.TIKTOK,
    "instagram.com": Platform.INSTAGRAM_REELS,
    "facebook.com": Platform.FACEBOOK_REELS,
    "fb.watch": Platform.FACEBOOK_REELS,
}


def _platform_of(permalink: str) -> Platform | None:
    host = (urlparse(permalink).hostname or "").removeprefix("www.").removeprefix("m.")
    return _PLATFORM_BY_HOST.get(host)


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    )


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _metadata(request: PublishRequest) -> dict[str, Any]:
    if request.platform is Platform.YOUTUBE_SHORTS:
        return {
            "youtube": {
                "title": request.title or request.text.splitlines()[0][:100],
                "categoryId": YOUTUBE_CATEGORY_ID,
                "madeForKids": bool(request.made_for_kids),
                "privacy": "public",
                "isAiGenerated": request.ai_generated,
            }
        }
    if request.platform is Platform.TIKTOK:
        return {"tiktok": {"isAiGenerated": request.ai_generated}}
    if request.platform is Platform.INSTAGRAM_REELS:
        return {
            "instagram": {
                "type": "reel",
                "shouldShareToFeed": True,
                "isAiGenerated": request.ai_generated,
            }
        }
    return {"facebook": {"type": "reel"}}


class BufferPublisher:
    provider = "buffer"

    def __init__(
        self,
        *,
        api_key: str,
        organization_id: str | None = None,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not api_key:
            raise ValueError("the Buffer API key is not configured")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._organization_id = organization_id
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0))
        self._clock = clock

    # --- Publisher port ---------------------------------------------------------

    def schedule(self, request: PublishRequest) -> PublishReceipt:
        if request.scheduled_at is None:
            raise ValueError("schedule() needs scheduled_at; use publish() to post now")
        return self._create(request, mode="customScheduled")

    def publish(self, request: PublishRequest) -> PublishReceipt:
        return self._create(request, mode="shareNow")

    def cancel(self, external_id: str) -> None:
        result = self._graphql(_DELETE, {"input": {"id": external_id}})["deletePost"]
        if "message" in result:
            raise PublishTargetError(
                f"Buffer would not delete post {external_id}: {result['message']}"
            )

    def get_status(self, external_id: str) -> PublicationStatus:
        post = self._graphql(_POST, {"input": {"id": external_id}})["post"]
        if post is None:
            return PublicationStatus(state=PublicationState.CANCELLED)
        state = _STATE_BY_STATUS.get(post["status"])
        if state is None:
            raise PublisherError(f"unknown Buffer post status {post['status']!r}", retryable=False)
        if state is PublicationState.FAILED:
            return PublicationStatus(state=state, error=(post.get("error") or {}).get("message"))
        if state is PublicationState.PUBLISHED:
            permalink = post.get("externalLink")
            platform = _PLATFORM_BY_SERVICE.get(post.get("channelService") or "") or (
                _platform_of(permalink) if permalink else None
            )
            return PublicationStatus(
                state=state,
                permalink=permalink,
                platform_post_id=(
                    parse_platform_post_id(platform, permalink) if platform and permalink else None
                ),
                published_at=_parse_time(post.get("sentAt")),
            )
        return PublicationStatus(state=state)

    def list_channels(self) -> list[PublisherChannel]:
        channels = self._graphql(_CHANNELS, {"input": {"organizationId": self._organization()}})[
            "channels"
        ]
        return [
            PublisherChannel(
                id=channel["id"],
                platform=_PLATFORM_BY_SERVICE[channel["service"]],
                name=channel.get("name") or channel["id"],
                external_account_id=channel.get("serviceId"),
                automatic=not (
                    (channel.get("metadata") or {}).get("defaultToReminders")
                    or channel.get("isDisconnected")
                    or channel.get("isLocked")
                ),
            )
            for channel in channels
            if channel.get("service") in _PLATFORM_BY_SERVICE
        ]

    # --- internals ------------------------------------------------------------------

    def _create(self, request: PublishRequest, *, mode: str) -> PublishReceipt:
        existing = self._find_existing(request)
        if existing is not None:
            return PublishReceipt(provider=self.provider, external_id=existing)

        post_input: dict[str, Any] = {
            "channelId": request.platform_account_id,
            "text": request.text,
            "schedulingType": "automatic",
            "mode": mode,
            "assets": [
                {
                    "video": {
                        "url": request.media_url,
                        "metadata": {"thumbnailOffset": request.thumbnail_offset_ms},
                    }
                }
            ],
            "metadata": _metadata(request),
        }
        if request.scheduled_at is not None and mode == "customScheduled":
            post_input["dueAt"] = _iso(request.scheduled_at)
        result = self._graphql(_CREATE, {"input": post_input})["createPost"]
        post = result.get("post")
        if post is None:
            raise PublishTargetError(
                f"Buffer refused the {request.platform.value} post: "
                f"{result.get('message', 'no reason given')}"
            )
        if post.get("schedulingType") != "automatic":
            # Buffer would only remind a phone to post it. Remove it and say why.
            self.cancel(post["id"])
            raise PublishTargetError(
                f"Buffer channel {request.platform_account_id} would only send a phone reminder "
                "instead of publishing; turn off 'Enable notifications by default' for it"
            )
        return PublishReceipt(provider=self.provider, external_id=post["id"])

    def _find_existing(self, request: PublishRequest) -> str | None:
        """Look for a post this publication already created (same channel, due
        time and text), e.g. when an earlier response was lost."""
        post_filter: dict[str, Any] = {"channelIds": [request.platform_account_id]}
        if request.scheduled_at is not None:
            due = _iso(request.scheduled_at)
            post_filter["dueAt"] = {
                "start": due,
                "end": _iso(request.scheduled_at + timedelta(seconds=1)),
            }
        else:
            post_filter["createdAt"] = {"start": _iso(self._clock() - timedelta(minutes=30))}
        data = self._graphql(
            _POSTS, {"input": {"organizationId": self._organization(), "filter": post_filter}}
        )
        wanted_due = request.scheduled_at
        for edge in data["posts"]["edges"]:
            post = edge["node"]
            if post.get("text") != request.text:
                continue
            if wanted_due is not None and _parse_time(post.get("dueAt")) != wanted_due:
                continue
            return str(post["id"])
        return None

    def _organization(self) -> str:
        if self._organization_id is None:
            organizations = self._graphql(_ORGANIZATIONS, {})["account"]["organizations"]
            if not organizations:
                raise PublisherError("the Buffer account has no organization", retryable=False)
            self._organization_id = organizations[0]["id"]
        return self._organization_id

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                API_URL, json={"query": query, "variables": variables}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise PublisherError(f"could not reach Buffer: {exc}") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            raise PublisherError(f"Buffer rate limit reached; retry after {retry_after}s")
        if response.status_code >= 400:
            raise PublisherError(
                f"Buffer returned HTTP {response.status_code}: {response.text[:300]}",
                retryable=response.status_code >= 500,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PublisherError("Buffer returned a non-JSON response") from exc
        if body.get("errors"):
            messages = "; ".join(str(e.get("message")) for e in body["errors"])
            raise PublisherError(f"Buffer GraphQL error: {messages}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise PublisherError("Buffer returned no data")
        return data
