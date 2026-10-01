"""V1 video fitness: a transparent, platform-specific heuristic.

Fitness asks "how did this video do *relative to what this account normally
does at this age*", using audience-quality signals — how much was watched,
whether it was finished, shared, saved, followed — never raw reach by itself.

For each signal the platform reports:

    component = clip(log2(observed / reference), -2, +2) / 2        ∈ [-1, +1]

where the reference is the account's own baseline for that signal, or a
platform prior when the account has too little history. Then

    score = 0.5 + 0.5 × Σ weight × component                         ∈ [0, 1]

with per-platform weights that sum to 1. A signal that is not reported
contributes 0 (i.e. "assume baseline"): missing data pulls the score toward
the middle rather than being read as failure. Signals a platform disables for
children's content (YouTube comments and saves) carry no weight there.

Views enter only as *distribution efficiency* — views relative to the account
baseline, with a small capped weight — and a video with no audience-quality
signal at all cannot be scored. The result records every input, weight and
reference so it can be reproduced or re-derived when this formula changes.

Priors and weights are V1 judgement calls, to be replaced by pilot evidence.
"""

import math
from decimal import Decimal

from app.analytics.ports import NormalizedMetrics
from app.fitness.ports import FitnessInputs, FitnessResult
from app.platforms import Platform

MIN_VIEWS = 30
"""Below this, rates are noise."""
FULL_CONFIDENCE_VIEWS = 500

_QUALITY_SIGNALS = (
    "retention",
    "completion",
    "like_rate",
    "share_rate",
    "save_rate",
    "comment_rate",
    "follow_rate",
)

_WEIGHTS: dict[Platform, dict[str, float]] = {
    # Comments and saves are disabled on made-for-kids videos: no weight.
    Platform.YOUTUBE_SHORTS: {
        "retention": 0.40, "completion": 0.10, "like_rate": 0.10, "share_rate": 0.10,
        "follow_rate": 0.15, "distribution": 0.10, "cost": 0.05,
    },
    Platform.TIKTOK: {
        "retention": 0.25, "completion": 0.25, "like_rate": 0.10, "share_rate": 0.15,
        "save_rate": 0.05, "follow_rate": 0.10, "distribution": 0.05, "cost": 0.05,
    },
    # Instagram reports neither completion nor follows per Reel.
    Platform.INSTAGRAM_REELS: {
        "retention": 0.30, "like_rate": 0.10, "share_rate": 0.20, "save_rate": 0.15,
        "comment_rate": 0.05, "distribution": 0.15, "cost": 0.05,
    },
    Platform.FACEBOOK_REELS: {
        "retention": 0.30, "completion": 0.10, "like_rate": 0.10, "share_rate": 0.20,
        "comment_rate": 0.05, "follow_rate": 0.10, "distribution": 0.10, "cost": 0.05,
    },
}  # fmt: skip

# Used until an account has enough history of its own.
_PRIORS: dict[str, float] = {
    "retention": 0.60,
    "completion": 0.30,
    "like_rate": 0.03,
    "share_rate": 0.003,
    "save_rate": 0.005,
    "comment_rate": 0.002,
    "follow_rate": 0.002,
}


class InsufficientSignals(Exception):
    """There is not enough evidence to compute a fitness score."""


def _rates(metrics: NormalizedMetrics, duration_seconds: float | None) -> dict[str, float]:
    """Audience-quality signals derived from one set of normalized metrics."""
    signals: dict[str, float] = {}
    if metrics.average_watch_fraction is not None:
        signals["retention"] = metrics.average_watch_fraction
    elif metrics.average_watch_seconds is not None and duration_seconds:
        signals["retention"] = metrics.average_watch_seconds / duration_seconds
    if metrics.completion_rate is not None:
        signals["completion"] = metrics.completion_rate
    if metrics.views:
        for name, count in (
            ("like_rate", metrics.likes),
            ("share_rate", metrics.shares),
            ("save_rate", metrics.saves),
            ("comment_rate", metrics.comments),
            ("follow_rate", metrics.follows),
        ):
            if count is not None:
                signals[name] = count / metrics.views
    return signals


def _component(observed: float, reference: float) -> float:
    if observed <= 0:
        return -1.0
    return max(-2.0, min(2.0, math.log2(observed / reference))) / 2


class HeuristicFitnessEvaluator:
    name = "heuristic"
    version = "1"

    def evaluate(self, inputs: FitnessInputs) -> FitnessResult:
        metrics = inputs.metrics
        weights = _WEIGHTS[inputs.platform]
        observed = _rates(metrics, inputs.duration_seconds)
        observed = {name: value for name, value in observed.items() if name in weights}
        if not any(name in observed for name in _QUALITY_SIGNALS):
            raise InsufficientSignals(
                "no audience-quality signal was reported; fitness is never computed from "
                "views alone"
            )
        if metrics.views is not None and metrics.views < MIN_VIEWS:
            raise InsufficientSignals(
                f"only {metrics.views} views: rates are not meaningful below {MIN_VIEWS} views"
            )

        baseline = _rates(inputs.baseline, inputs.duration_seconds) if inputs.baseline else {}
        components: dict[str, float] = {}
        references: dict[str, dict[str, float | str]] = {}
        for name, value in observed.items():
            if baseline.get(name):
                reference, source = baseline[name], "account_baseline"
            else:
                reference, source = _PRIORS[name], "platform_prior"
            components[name] = _component(value, reference)
            references[name] = {"value": reference, "source": source}

        # Distribution efficiency: reach relative to what this account normally
        # gets. Only meaningful against the account's own history.
        if inputs.baseline is not None and inputs.baseline.views and metrics.views is not None:
            components["distribution"] = _component(metrics.views, inputs.baseline.views)
            references["distribution"] = {
                "value": float(inputs.baseline.views),
                "source": "account_baseline",
            }
        if inputs.target_cost_usd is not None and inputs.production_cost_usd > Decimal("0"):
            components["cost"] = _component(
                float(inputs.target_cost_usd), float(inputs.production_cost_usd)
            )
            references["cost"] = {"value": float(inputs.target_cost_usd), "source": "budget_target"}

        weighted = sum(weights[name] * value for name, value in components.items())
        coverage = sum(weights[name] for name in components)
        views = metrics.views if metrics.views is not None else FULL_CONFIDENCE_VIEWS
        confidence = min(1.0, views / FULL_CONFIDENCE_VIEWS) * coverage
        return FitnessResult(
            evaluator=self.name,
            evaluator_version=self.version,
            score=round(0.5 + 0.5 * weighted, 6),
            confidence=round(confidence, 6),
            components={name: round(value, 6) for name, value in components.items()},
            details={
                "weights": dict(weights),
                "references": references,
                "signals_missing": sorted(set(weights) - set(components)),
            },
            inputs=inputs,
        )
