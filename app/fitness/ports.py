"""Fitness evaluation port.

An evaluator turns one platform's normalized observation into a score. The
result echoes its inputs and the evaluator name/version so every fitness value
can be reproduced and re-derived when the formula changes.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.analytics.ports import NormalizedMetrics
from app.platforms import Platform


class FitnessInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    metrics: NormalizedMetrics
    hours_since_publication: float
    production_cost_usd: Decimal
    target_cost_usd: Decimal | None = None
    duration_seconds: float | None = None
    """Length of the video, to turn watch seconds into a watched fraction."""
    observed_at: datetime
    baseline: NormalizedMetrics | None = None
    """Typical values for the same platform account at the same maturity,
    when it has enough history. Without it, platform priors are used."""


class FitnessResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluator: str
    evaluator_version: str
    score: float
    """0–1, where 0.5 means "as good as this account's baseline"."""
    confidence: float = 1.0
    """0–1: how much evidence stands behind the score."""
    components: dict[str, float] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    inputs: FitnessInputs


class FitnessEvaluator(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def evaluate(self, inputs: FitnessInputs) -> FitnessResult: ...
