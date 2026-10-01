"""Model catalogue seed: Higgsfield API text-to-video models that can produce
vertical video.

Prices are public list prices read on 2026-10-01 (docs/research/higgsfield-api.md).
Entries with ``price_verified=False`` rest on an inference about how the vendor
prices audio. ``quality`` is Hatch's provisional 1–5 judgement, to be replaced
by pilot evidence (cost per accepted video, QA pass rate). The catalogue lives
in the ``provider_models`` table so it can be edited without a deploy.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.production.models import ProviderModel
from app.production.routing import ModelSpec

_VERTICAL = ("9:16", "16:9", "1:1")


def _usd(**prices: str) -> dict[str, Decimal]:
    return {resolution.lstrip("_"): Decimal(price) for resolution, price in prices.items()}


HIGGSFIELD_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        provider="higgsfield",
        model="alibaba/wan-3.0/text-to-video",
        min_duration_seconds=2,
        max_duration_seconds=30,
        aspect_ratios=_VERTICAL,
        native_audio=True,
        quality=3,
        usd_per_second=_usd(_480p="0.05", _720p="0.10", _1080p="0.20"),
    ),
    ModelSpec(
        provider="higgsfield",
        model="lightricks/ltx-2.5/text-to-video/fast",
        durations=(6, 8, 10),
        aspect_ratios=("9:16", "16:9"),
        native_audio=True,
        quality=2,
        usd_per_second=_usd(_720p="0.09", _1080p="0.13"),
    ),
    ModelSpec(
        provider="higgsfield",
        model="pixverse/v6/text-to-video",
        min_duration_seconds=1,
        max_duration_seconds=15,
        aspect_ratios=_VERTICAL,
        native_audio=True,
        quality=3,
        usd_per_second=_usd(_1080p="0.0978"),
    ),
    ModelSpec(
        provider="higgsfield",
        model="kling-video/v2.6/pro/text-to-video",
        durations=(5, 10),
        aspect_ratios=_VERTICAL,
        native_audio=True,
        quality=4,
        usd_per_second=_usd(default="0.14"),
        price_verified=False,
    ),
    ModelSpec(
        provider="higgsfield",
        model="kling-video/v3.0/std/text-to-video",
        min_duration_seconds=3,
        max_duration_seconds=15,
        aspect_ratios=_VERTICAL,
        native_audio=True,
        quality=4,
        usd_per_second=_usd(default="0.126"),
        price_verified=False,
    ),
    ModelSpec(
        provider="higgsfield",
        model="kling-video/v3.0-turbo/text-to-video",
        min_duration_seconds=3,
        max_duration_seconds=15,
        aspect_ratios=_VERTICAL,
        native_audio=False,
        quality=4,
        usd_per_second=_usd(_720p="0.112", _1080p="0.14"),
    ),
    ModelSpec(
        provider="higgsfield",
        model="bytedance/seedance-2.0/text-to-video",
        min_duration_seconds=4,
        max_duration_seconds=15,
        aspect_ratios=_VERTICAL,
        native_audio=True,
        quality=5,
        usd_per_second=_usd(_480p="0.1407"),
    ),
)


def seed_provider_models(session: Session) -> None:
    """Insert catalogue entries that are not in the database yet. Existing
    rows (which an operator may have edited or disabled) are left alone."""
    existing = {
        (row.provider, row.model, row.operation) for row in session.scalars(select(ProviderModel))
    }
    for spec in HIGGSFIELD_MODELS:
        if (spec.provider, spec.model, spec.operation) in existing:
            continue
        capabilities = spec.model_dump(
            mode="json", exclude={"provider", "model", "operation", "usd_per_second"}
        )
        session.add(
            ProviderModel(
                provider=spec.provider,
                model=spec.model,
                operation=spec.operation,
                capabilities=capabilities,
                pricing={"usd_per_second": {k: str(v) for k, v in spec.usd_per_second.items()}},
            )
        )
    session.flush()
