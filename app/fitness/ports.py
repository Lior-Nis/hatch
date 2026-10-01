"""Fitness evaluation port.

An evaluator turns one platform's normalized observation into a score. The
result echoes its inputs and the evaluator name/version so every fitness value
can be reproduced and re-derived when the formula changes.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.analytics.ports import NormalizedMetrics
from app.platforms import Platform


class FitnessInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    platform: Platform
    metrics: NormalizedMetrics
    hours_since_publication: float
    production_cost_usd: Decimal
    observed_at: datetime
    baseline: NormalizedMetrics | None = None
    """Historical baseline for the same platform account, when available."""


class FitnessResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluator: str
    evaluator_version: str
    score: float
    components: dict[str, float] = Field(default_factory=dict)
    inputs: FitnessInputs


class FitnessEvaluator(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def evaluate(self, inputs: FitnessInputs) -> FitnessResult: ...
