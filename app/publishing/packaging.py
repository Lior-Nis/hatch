"""Platform-specific packaging of one canonical video.

The video itself is never re-rendered per platform. What varies is the wrapper:
title, caption, hashtags, call to action, thumbnail frame and policy flags.
Packages are built deterministically from the IP and the genome, and validated
against each platform's rules before anything is sent to a publisher.
"""

import re

from pydantic import BaseModel, ConfigDict

from app.creative.genome import Genes
from app.platforms import Platform


class PackagingError(ValueError):
    pass


class PackagingRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    title_required: bool
    max_title: int
    max_text: int
    max_hashtags: int
    made_for_kids_required: bool = False


PLATFORM_RULES: dict[Platform, PackagingRules] = {
    Platform.YOUTUBE_SHORTS: PackagingRules(
        title_required=True,
        max_title=100,
        max_text=5000,
        max_hashtags=3,
        made_for_kids_required=True,
    ),
    Platform.TIKTOK: PackagingRules(
        title_required=False, max_title=0, max_text=2200, max_hashtags=5
    ),
    Platform.INSTAGRAM_REELS: PackagingRules(
        title_required=False, max_title=0, max_text=2200, max_hashtags=8
    ),
    Platform.FACEBOOK_REELS: PackagingRules(
        title_required=False, max_title=0, max_text=2200, max_hashtags=3
    ),
}

_CATEGORY_TAGS = {
    "narrative_adventure": ("KidsStories", "StoryTime", "CalmKidsVideos", "BedtimeStories"),
    "learning_problem_solving": ("LearningForKids", "KidsLearning", "ProblemSolving", "Preschool"),
    "humor_imagination": ("FunnyKidsVideos", "Imagination", "SillyFun", "KidsComedy"),
}
_PLATFORM_TAGS = {
    Platform.YOUTUBE_SHORTS: ("Shorts",),
    Platform.TIKTOK: ("KidsOfTikTok", "ParentsOfTikTok"),
    Platform.INSTAGRAM_REELS: ("KidsReels", "ParentingReels", "ScreenTimeDoneRight"),
    Platform.FACEBOOK_REELS: (),
}
# What each platform's audience is asked to do, by cta_type gene.
_CTA = {
    "follow": {
        Platform.YOUTUBE_SHORTS: "Subscribe for a new {ip} story every day.",
        Platform.TIKTOK: "Follow for a new {ip} story every day.",
        Platform.INSTAGRAM_REELS: "Follow for a new {ip} story every day.",
        Platform.FACEBOOK_REELS: "Follow the page for a new {ip} story every day.",
    },
    "share": dict.fromkeys(Platform, "Share it with a little one who would love {ip}."),
    "watch_next": dict.fromkeys(Platform, "More {ip} stories are on our page."),
}
_HASHTAG = re.compile(r"^[A-Za-z0-9_]+$")


class PlatformPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    title: str | None
    caption: str
    hashtags: tuple[str, ...]
    cta: str | None
    thumbnail_offset_ms: int
    made_for_kids: bool | None
    """Only YouTube has this flag; content for ages 4–8 must set it."""
    ai_generated: bool

    def text(self) -> str:
        """The post body: caption, call to action, then hashtags."""
        parts = [self.caption]
        if self.cta:
            parts.append(self.cta)
        parts.append(" ".join(f"#{tag}" for tag in self.hashtags))
        return "\n\n".join(parts)


def _sentence(text: str) -> str:
    text = text.strip().rstrip(".")
    return text[:1].upper() + text[1:]


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def build_packages(*, ip_name: str, category: str, genes: Genes) -> dict[Platform, PlatformPackage]:
    """One package per platform for the same canonical video."""
    topic = _sentence(genes.topic)
    ip_tag = re.sub(r"[^A-Za-z0-9]", "", ip_name)
    category_tags = _CATEGORY_TAGS.get(category, ("KidsVideos",))
    lesson = f" Little ones find out: {genes.educational_goal}." if genes.educational_goal else ""
    captions = {
        Platform.YOUTUBE_SHORTS: f"{topic}. A gentle {ip_name} story for little ones.{lesson}",
        Platform.TIKTOK: (
            f"{topic} ✨ A calm {genes.duration_seconds}-second {ip_name} story for ages 4–8."
        ),
        Platform.INSTAGRAM_REELS: (
            f"{topic}. A calm, kind {ip_name} story made for ages 4–8 — screen time you can "
            f"feel good about.{lesson}"
        ),
        Platform.FACEBOOK_REELS: f"{topic}. A gentle {ip_name} story for ages 4–8.{lesson}",
    }
    packages = {}
    for platform, rules in PLATFORM_RULES.items():
        tags = (ip_tag, *_PLATFORM_TAGS[platform], *category_tags)
        hashtags = tuple(dict.fromkeys(tags))[: rules.max_hashtags]
        title = None
        if rules.title_required:
            suffix = f" | {ip_name}"
            title = _trim(topic, rules.max_title - len(suffix)) + suffix
        cta_template = _CTA.get(genes.cta_type, {}).get(platform)
        packages[platform] = PlatformPackage(
            platform=platform,
            title=title,
            caption=_trim(captions[platform], rules.max_text - 400),
            hashtags=hashtags,
            cta=cta_template.format(ip=ip_name) if cta_template else None,
            thumbnail_offset_ms=min(2000, genes.duration_seconds * 500),
            made_for_kids=True if rules.made_for_kids_required else None,
            ai_generated=True,
        )
    return packages


def validate_package(package: PlatformPackage) -> None:
    rules = PLATFORM_RULES[package.platform]
    name = package.platform.value
    if rules.title_required and not package.title:
        raise PackagingError(f"{name}: a title is required")
    if package.title and len(package.title) > rules.max_title:
        raise PackagingError(f"{name}: title is longer than {rules.max_title} characters")
    if not package.caption.strip():
        raise PackagingError(f"{name}: caption is empty")
    if len(package.text()) > rules.max_text:
        raise PackagingError(f"{name}: text is longer than {rules.max_text} characters")
    if not 1 <= len(package.hashtags) <= rules.max_hashtags:
        raise PackagingError(f"{name}: between 1 and {rules.max_hashtags} hashtags are allowed")
    for tag in package.hashtags:
        if not _HASHTAG.match(tag):
            raise PackagingError(f"{name}: {tag!r} is not a valid hashtag")
    if rules.made_for_kids_required and package.made_for_kids is not True:
        raise PackagingError(f"{name}: videos for ages 4–8 must be marked made for kids")
    if package.thumbnail_offset_ms < 0:
        raise PackagingError(f"{name}: thumbnail offset is negative")
