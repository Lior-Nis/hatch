from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.production.catalog import HIGGSFIELD_MODELS, seed_provider_models
from app.production.models import ProviderModel
from app.production.routing import (
    ModelSpec,
    NoEligibleModel,
    RoutingRequirements,
    load_catalog,
    route_video_model,
)

CATALOG = list(HIGGSFIELD_MODELS)
WAN = "alibaba/wan-3.0/text-to-video"


def requirements(**overrides: object) -> RoutingRequirements:
    fields: dict[str, object] = {
        "duration_seconds": 8,
        "aspect_ratio": "9:16",
        "with_audio": True,
        "target_cost_usd": Decimal("0.50"),
        "max_cost_usd": Decimal("0.75"),
    }
    fields.update(overrides)
    return RoutingRequirements.model_validate(fields)


def test_router_picks_a_model_that_fits_the_target_cost() -> None:
    decision = route_video_model(requirements(), CATALOG)

    assert decision.model == WAN
    assert decision.resolution == "480p"
    assert decision.estimated_cost_usd == Decimal("0.40")
    assert decision.estimated_cost_usd <= Decimal("0.50")
    assert "target" in decision.reason


def test_a_bigger_budget_buys_a_higher_resolution() -> None:
    decision = route_video_model(
        requirements(target_cost_usd=Decimal("0.90"), max_cost_usd=Decimal("1.00")), CATALOG
    )

    assert decision.resolution != "480p"
    assert decision.estimated_cost_usd <= Decimal("0.90")


def test_models_that_cannot_meet_the_requirements_are_excluded_with_a_reason() -> None:
    decision = route_video_model(requirements(), CATALOG)

    rejected = {entry.model: entry.rejected_because for entry in decision.considered}
    assert "duration" in (rejected["kling-video/v2.6/pro/text-to-video"] or "")  # only 5s / 10s
    assert "audio" in (rejected["kling-video/v3.0-turbo/text-to-video"] or "")
    assert rejected[WAN] is None


def test_when_the_target_is_unreachable_the_cheapest_model_under_the_ceiling_is_used() -> None:
    decision = route_video_model(
        requirements(target_cost_usd=Decimal("0.10"), max_cost_usd=Decimal("0.75")), CATALOG
    )

    assert decision.model == WAN
    assert decision.estimated_cost_usd == Decimal("0.40")
    assert "above the target" in decision.reason


def test_no_model_under_the_hard_ceiling_is_an_error() -> None:
    with pytest.raises(NoEligibleModel, match=r"0\.20"):
        route_video_model(
            requirements(target_cost_usd=Decimal("0.10"), max_cost_usd=Decimal("0.20")), CATALOG
        )


def test_an_unreliable_model_loses_to_a_reliable_alternative() -> None:
    reliable = route_video_model(requirements(max_cost_usd=Decimal("1.00")), CATALOG)
    flaky = route_video_model(
        requirements(max_cost_usd=Decimal("1.00")), CATALOG, failure_rates={WAN: 0.9}
    )

    assert reliable.model == WAN
    assert flaky.model != WAN
    assert "failure rate" in next(e for e in flaky.considered if e.model == WAN).note


def test_fallbacks_are_ranked_alternatives_within_the_ceiling() -> None:
    decision = route_video_model(requirements(max_cost_usd=Decimal("1.00")), CATALOG)

    assert decision.fallbacks
    assert all(fallback.model != decision.model or fallback.resolution != decision.resolution
               for fallback in decision.fallbacks)  # fmt: skip
    assert all(fallback.estimated_cost_usd <= Decimal("1.00") for fallback in decision.fallbacks)


def test_configured_override_wins_and_is_recorded_as_the_reason() -> None:
    override = "lightricks/ltx-2.5/text-to-video/fast"

    decision = route_video_model(
        requirements(max_cost_usd=Decimal("1.00")), CATALOG, override=override
    )

    assert decision.model == override
    assert "override" in decision.reason


def test_an_override_that_cannot_do_the_job_is_refused() -> None:
    with pytest.raises(NoEligibleModel, match="override"):
        route_video_model(requirements(), CATALOG, override="kling-video/v2.6/pro/text-to-video")
    with pytest.raises(NoEligibleModel, match="not in the catalogue"):
        route_video_model(requirements(), CATALOG, override="made-up/model")


def test_routing_is_deterministic() -> None:
    assert route_video_model(requirements(), CATALOG) == route_video_model(requirements(), CATALOG)


def test_higher_quality_is_preferred_when_cost_is_equal() -> None:
    def spec(model: str, quality: int) -> ModelSpec:
        return ModelSpec(
            provider="p", model=model, min_duration_seconds=2, max_duration_seconds=30,
            aspect_ratios=("9:16",), native_audio=True, quality=quality,
            usd_per_second={"720p": Decimal("0.05")},
        )  # fmt: skip

    decision = route_video_model(requirements(), [spec("plain", 2), spec("better", 4)])

    assert decision.model == "better"


def test_catalogue_is_seeded_once_and_loaded_from_the_database(session: Session) -> None:
    seed_provider_models(session)
    seed_provider_models(session)

    assert session.scalar(select(func.count()).select_from(ProviderModel)) == len(CATALOG)
    loaded = load_catalog(session)
    assert {spec.model for spec in loaded} == {spec.model for spec in CATALOG}
    assert route_video_model(requirements(), loaded).model == WAN


def test_disabled_models_are_not_routed_to(session: Session) -> None:
    seed_provider_models(session)
    session.scalars(select(ProviderModel).where(ProviderModel.model == WAN)).one().enabled = False
    session.flush()

    decision = route_video_model(requirements(max_cost_usd=Decimal("1.00")), load_catalog(session))

    assert decision.model != WAN
