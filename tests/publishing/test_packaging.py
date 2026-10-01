import pytest

from app.experiments.fixtures import FIRST_SHORT
from app.ips.catalog import PUZZLE_POND
from app.platforms import Platform
from app.publishing.packaging import (
    PLATFORM_RULES,
    PackagingError,
    PlatformPackage,
    build_packages,
    validate_package,
)

GENES = FIRST_SHORT.genome.genes


def packages(**gene_changes: object) -> dict[Platform, PlatformPackage]:
    genes = GENES.model_copy(update=gene_changes)
    return build_packages(ip_name="Nibbin Hollow", category="narrative_adventure", genes=genes)


def test_one_video_gets_a_valid_package_for_each_of_the_four_platforms() -> None:
    built = packages()

    assert set(built) == set(Platform)
    for package in built.values():
        validate_package(package)  # does not raise


def test_packages_differ_by_platform_but_describe_the_same_video() -> None:
    built = packages()

    assert len({package.text() for package in built.values()}) == 4
    assert all("glow" in package.text().lower() for package in built.values())
    assert all("NibbinHollow" in package.hashtags for package in built.values())


def test_youtube_package_has_a_title_is_made_for_kids_and_tagged_shorts() -> None:
    youtube = packages()[Platform.YOUTUBE_SHORTS]

    assert youtube.title is not None and "Nibbin Hollow" in youtube.title
    assert len(youtube.title) <= 100
    assert youtube.made_for_kids is True
    assert "Shorts" in youtube.hashtags


def test_only_youtube_carries_the_made_for_kids_flag() -> None:
    built = packages()

    assert [p for p, pkg in built.items() if pkg.made_for_kids is not None] == [
        Platform.YOUTUBE_SHORTS
    ]


def test_ai_generation_is_disclosed_on_every_platform() -> None:
    assert all(package.ai_generated for package in packages().values())


def test_hashtag_counts_respect_each_platforms_limit() -> None:
    for platform, package in packages().items():
        assert 1 <= len(package.hashtags) <= PLATFORM_RULES[platform].max_hashtags
        assert len(set(package.hashtags)) == len(package.hashtags)


def test_call_to_action_follows_the_cta_gene() -> None:
    assert all(package.cta is None for package in packages(cta_type="none").values())
    follow = packages(cta_type="follow")
    assert all(package.cta for package in follow.values())
    assert "Subscribe" in (follow[Platform.YOUTUBE_SHORTS].cta or "")
    assert "Follow" in (follow[Platform.TIKTOK].cta or "")


def test_thumbnail_frame_is_inside_the_video() -> None:
    for package in packages(duration_seconds=6).values():
        assert 0 < package.thumbnail_offset_ms < 6000


def test_a_long_topic_is_trimmed_to_fit_the_title_limit() -> None:
    youtube = packages(topic="why " + "very " * 60 + "long topics need trimming")[
        Platform.YOUTUBE_SHORTS
    ]

    assert youtube.title is not None and len(youtube.title) <= 100
    validate_package(youtube)


def test_category_changes_the_hashtags() -> None:
    learning = build_packages(
        ip_name=PUZZLE_POND.ip.name, category=PUZZLE_POND.ip.category, genes=GENES
    )

    assert set(learning[Platform.TIKTOK].hashtags) != set(packages()[Platform.TIKTOK].hashtags)
    assert "PuzzlePond" in learning[Platform.TIKTOK].hashtags


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"title": None}, "title"),
        ({"title": "x" * 101}, "title"),
        ({"hashtags": ("a", "b", "c", "d")}, "hashtags"),
        ({"hashtags": ("two words",)}, "hashtag"),
        ({"caption": ""}, "caption"),
        ({"made_for_kids": False}, "made for kids"),
    ],
)
def test_invalid_youtube_packages_are_rejected(change: dict[str, object], problem: str) -> None:
    package = packages()[Platform.YOUTUBE_SHORTS].model_copy(update=change)

    with pytest.raises(PackagingError, match=problem):
        validate_package(package)
