"""Evolution policy: the thresholds behind selection, replication and IP
lifecycle decisions. One versioned object so every decision can name the
policy it was made under."""

from pydantic import BaseModel, ConfigDict

from app.platforms import Platform

POLICY_VERSION = "evolution-1"


class EvolutionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = POLICY_VERSION

    # --- evidence ---
    min_confidence: float = 0.2
    """Fitness confidence below this is not evidence."""
    platform_weights: dict[Platform, float] = {
        Platform.YOUTUBE_SHORTS: 0.4,
        Platform.TIKTOK: 0.2,
        Platform.INSTAGRAM_REELS: 0.2,
        Platform.FACEBOOK_REELS: 0.2,
    }

    # --- winners and replication ---
    winner_threshold: float = 0.65
    """Experiment fitness at or above this makes a *potential* winner."""
    support_threshold: float = 0.55
    """A replication descendant at or above this reproduces the effect."""
    anomalous_threshold: float = 0.80
    """An original this high whose descendants all fail was noise."""
    replication_descendants: int = 3
    replication_max_descendants: int = 5

    # --- allocation ---
    exploit_share: float = 0.60
    mutate_share: float = 0.25
    explore_share: float = 0.15
    allocation_window: int = 20
    """How many recent allocations the shares are balanced over."""
    max_lineage_share: float = 0.5
    """No lineage may take more than this share of recent descendant slots."""
    promising_threshold: float = 0.50
    """Experiments at or above this may be mutated."""

    # --- IP lifecycle ---
    ip_promising_score: float = 0.55
    ip_scaled_supported_lineages: int = 2
    ip_archive_score: float = 0.40
    ip_archive_min_videos: int = 10
    ip_resurrect_score: float = 0.55
