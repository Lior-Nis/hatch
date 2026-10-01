"""Dynamic model routing.

Chooses the video model and resolution for a candidate from what it needs
(duration, aspect ratio, audio) and what Hatch can afford (target and ceiling
cost), preferring quality and reliability. The router is a pure, deterministic
function of its inputs, and its decision — including every model it rejected
and why — is stored with the candidate, so "why this model?" is always
answerable.

Catalogue prices are planning figures. The provider's own estimate, checked by
the budget governor, is what actually authorises spend.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.production.models import ProviderModel

ROUTER_VERSION = "1"

_RESOLUTION_RANK = {"360p": 0, "480p": 1, "720p": 2, "default": 2, "1080p": 3, "4k": 4}
_UNRELIABLE_AT = 0.5


class NoEligibleModel(Exception):
    pass


class ModelSpec(BaseModel):
    """What routing needs to know about one model."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    operation: str = "text_to_video"
    min_duration_seconds: int = 1
    max_duration_seconds: int = 60
    durations: tuple[int, ...] = ()
    """When non-empty, the only durations the model accepts."""
    aspect_ratios: tuple[str, ...]
    native_audio: bool
    quality: int = Field(ge=1, le=5)
    """Hatch's own 1–5 judgement of output quality. An assumption until pilot
    evidence replaces it."""
    usd_per_second: dict[str, Decimal]
    """Price per second by resolution, for the configuration Hatch uses (with
    audio when the model has it). "default" for models with one tier."""
    price_verified: bool = True

    def duration_problem(self, seconds: int) -> str | None:
        if self.durations:
            if seconds not in self.durations:
                allowed = ", ".join(f"{d}s" for d in self.durations)
                return f"duration {seconds}s not supported (allowed: {allowed})"
            return None
        if not self.min_duration_seconds <= seconds <= self.max_duration_seconds:
            return (
                f"duration {seconds}s not supported "
                f"({self.min_duration_seconds}–{self.max_duration_seconds}s)"
            )
        return None


class RoutingRequirements(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_seconds: int = Field(gt=0)
    aspect_ratio: str = "9:16"
    with_audio: bool = True
    target_cost_usd: Decimal
    max_cost_usd: Decimal


class RouteOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    resolution: str
    estimated_cost_usd: Decimal


class ConsideredModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    rejected_because: str | None = None
    note: str = ""


class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    router_version: str = ROUTER_VERSION
    provider: str
    model: str
    resolution: str
    estimated_cost_usd: Decimal
    reason: str
    fallbacks: tuple[RouteOption, ...]
    considered: tuple[ConsideredModel, ...]
    requirements: RoutingRequirements


def _money(value: Decimal) -> str:
    return f"${value:.2f}"


def route_video_model(
    requirements: RoutingRequirements,
    catalog: Sequence[ModelSpec],
    *,
    failure_rates: Mapping[str, float] | None = None,
    override: str | None = None,
) -> RoutingDecision:
    failure_rates = failure_rates or {}
    considered: list[ConsideredModel] = []
    options: list[tuple[ModelSpec, RouteOption]] = []

    for spec in sorted(catalog, key=lambda s: s.model):
        problem = spec.duration_problem(requirements.duration_seconds)
        if problem is None and requirements.aspect_ratio not in spec.aspect_ratios:
            problem = f"aspect ratio {requirements.aspect_ratio} not supported"
        if problem is None and requirements.with_audio and not spec.native_audio:
            problem = "no native audio"
        affordable = []
        if problem is None:
            priced = [
                RouteOption(
                    provider=spec.provider,
                    model=spec.model,
                    resolution=resolution,
                    estimated_cost_usd=price * requirements.duration_seconds,
                )
                for resolution, price in spec.usd_per_second.items()
            ]
            affordable = [o for o in priced if o.estimated_cost_usd <= requirements.max_cost_usd]
            if not affordable:
                cheapest = min(o.estimated_cost_usd for o in priced)
                problem = (
                    f"no configuration under the {_money(requirements.max_cost_usd)} ceiling "
                    f"(cheapest {_money(cheapest)})"
                )
        failure = failure_rates.get(spec.model, 0.0)
        considered.append(
            ConsideredModel(
                model=spec.model,
                rejected_because=problem,
                note=f"failure rate {failure:.0%}" if failure else "",
            )
        )
        options += [(spec, option) for option in affordable]

    def rank(entry: tuple[ModelSpec, RouteOption]) -> tuple[object, ...]:
        spec, option = entry
        failure = failure_rates.get(spec.model, 0.0)
        within_target = option.estimated_cost_usd <= requirements.target_cost_usd
        value = spec.quality + _RESOLUTION_RANK.get(option.resolution, 2) - 5 * failure
        return (
            failure >= _UNRELIABLE_AT,  # reliable models first
            not within_target,  # then those that fit the target cost
            -value if within_target else option.estimated_cost_usd,  # best value, or cheapest
            option.estimated_cost_usd,
            option.model,
            option.resolution,
        )

    ranked = sorted(options, key=rank)

    if override is not None:
        known = {spec.model for spec in catalog}
        if override not in known:
            raise NoEligibleModel(f"override {override} is not in the catalogue")
        ranked_override = [entry for entry in ranked if entry[1].model == override]
        if not ranked_override:
            why = next(c.rejected_because for c in considered if c.model == override)
            raise NoEligibleModel(f"override {override} cannot be used: {why}")
        chosen = ranked_override[0][1]
        reason = f"config override ({override})"
    else:
        if not ranked:
            raise NoEligibleModel(
                f"no model can produce a {requirements.duration_seconds}s "
                f"{requirements.aspect_ratio} video"
                f"{' with audio' if requirements.with_audio else ''} under "
                f"{_money(requirements.max_cost_usd)}"
            )
        chosen = ranked[0][1]
        if chosen.estimated_cost_usd <= requirements.target_cost_usd:
            reason = (
                f"best quality and reliability within the {_money(requirements.target_cost_usd)} "
                f"target ({_money(chosen.estimated_cost_usd)})"
            )
        else:
            reason = (
                f"cheapest eligible option at {_money(chosen.estimated_cost_usd)}, above the "
                f"target of {_money(requirements.target_cost_usd)} but under the "
                f"{_money(requirements.max_cost_usd)} ceiling"
            )

    others = [option for _, option in ranked if option != chosen]
    # A fallback is for when the chosen model fails, so prefer different models.
    others.sort(key=lambda option: option.model == chosen.model)
    return RoutingDecision(
        provider=chosen.provider,
        model=chosen.model,
        resolution=chosen.resolution,
        estimated_cost_usd=chosen.estimated_cost_usd,
        reason=reason,
        fallbacks=tuple(others[:3]),
        considered=tuple(considered),
        requirements=requirements,
    )


def load_catalog(session: Session, *, operation: str = "text_to_video") -> list[ModelSpec]:
    """Enabled models from the ``provider_models`` table."""
    rows = session.scalars(
        select(ProviderModel).where(
            ProviderModel.enabled.is_(True), ProviderModel.operation == operation
        )
    )
    return [
        ModelSpec.model_validate(
            {
                "provider": row.provider,
                "model": row.model,
                "operation": row.operation,
                **row.capabilities,
                "usd_per_second": row.pricing["usd_per_second"],
            }
        )
        for row in rows
    ]
