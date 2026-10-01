"""Machine-readable hypotheses.

A hypothesis is only useful if evidence can refute it. ``Prediction`` pins a
hypothesis to a metric, a direction, a baseline, a minimum effect size and the
genes whose change is claimed to cause it.
"""

import uuid
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creative.genome import GENE_SPECS
from app.platforms import Platform


class Metric(StrEnum):
    COMPLETION_RATE = "completion_rate"
    AVERAGE_WATCH_FRACTION = "average_watch_fraction"
    AVERAGE_WATCH_SECONDS = "average_watch_seconds"
    VIEWS_PER_IMPRESSION = "views_per_impression"
    LIKES_PER_VIEW = "likes_per_view"
    SHARES_PER_VIEW = "shares_per_view"
    SAVES_PER_VIEW = "saves_per_view"
    FOLLOWS_PER_VIEW = "follows_per_view"


class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: Metric
    direction: Literal["increase", "decrease"]
    compared_to: Literal["parent", "ip_baseline", "lineage_baseline"]
    minimum_relative_effect: float = Field(gt=0, le=10)
    """Smallest relative change that would count as support (0.1 = 10%)."""
    genes_under_test: tuple[str, ...] = Field(min_length=1)
    platforms: tuple[Platform, ...] = ()
    """Empty means every platform the video is published to."""
    evidence_experiment_ids: tuple[uuid.UUID, ...] = ()
    """Earlier experiments whose results informed this hypothesis."""

    @model_validator(mode="after")
    def _genes_exist(self) -> Self:
        unknown = set(self.genes_under_test) - set(GENE_SPECS)
        if unknown:
            raise ValueError(f"genes_under_test names unknown genes: {', '.join(sorted(unknown))}")
        return self
