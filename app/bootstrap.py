"""Composition root: build concrete adapters from settings.

The only place that knows which vendor implements which port.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import time, timedelta
from decimal import Decimal

import anthropic
from sqlalchemy.orm import Session

from app.analytics.jobs import analytics_handlers
from app.analytics.ports import AnalyticsAdapter
from app.budgets.governor import BudgetGovernor, BudgetLimits
from app.config import Settings
from app.creative.candidates import routed_production
from app.creative.jobs import creative_handlers
from app.db import make_engine, registry
from app.evolution.anti_cloning import AntiCloningPolicy
from app.experiments.spec import OutputRequirements
from app.fitness.heuristic import HeuristicFitnessEvaluator
from app.fitness.jobs import fitness_handlers
from app.llm.calls import bound_caller
from app.llm.ports import LanguageModel
from app.platforms import Platform
from app.production.jobs import PRODUCE_SHORT, produce_short_handler
from app.production.ports import MediaGenerator
from app.production.run import ProductionDeps, RetryPolicy
from app.publishing.jobs import publishing_handlers
from app.publishing.ports import Publisher
from app.publishing.schedule import PostingSchedule
from app.quality.content import ContentReviewer, ContentThresholds, content_gates
from app.quality.jobs import RUN_QA, GateFactory, run_qa_handler
from app.quality.ports import QAGate
from app.quality.technical import TechnicalQAGate
from app.scheduling.worker import JobHandler
from app.storage import AssetStore
from integrations.anthropic.language_model import AnthropicLanguageModel
from integrations.buffer.publisher import BufferPublisher
from integrations.credentials import TokenStore
from integrations.fake.media import FakeMediaGenerator
from integrations.higgsfield.generator import HiggsfieldMediaGenerator
from integrations.meta.analytics import FacebookReelsAnalytics, InstagramReelsAnalytics
from integrations.object_storage.local import LocalAssetStore
from integrations.object_storage.s3 import S3AssetStore
from integrations.tiktok.analytics import TikTokBusinessAnalytics, TikTokDisplayAnalytics
from integrations.youtube.analytics import YouTubeAnalytics

assert registry  # every ORM model must be registered before any session is used

logger = logging.getLogger(__name__)

# What a Short must satisfy unless a candidate says otherwise.
DEFAULT_OUTPUT_REQUIREMENTS = OutputRequirements(
    aspect_ratio="9:16",
    min_duration_seconds=4,
    max_duration_seconds=15,
    min_height=800,
    audio_expected=True,
)


class ConfigurationError(Exception):
    """A required setting or credential is missing."""


@contextmanager
def open_session(settings: Settings) -> Iterator[Session]:
    """A session for one unit of CLI work; releases its connections on exit."""
    engine = make_engine(settings.database_url)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def build_asset_store(settings: Settings) -> AssetStore:
    if settings.asset_store == "local":
        return LocalAssetStore(settings.asset_dir, public_base_url=settings.asset_public_base_url)
    if (
        settings.s3_bucket is None
        or settings.s3_access_key_id is None
        or settings.s3_secret_access_key is None
    ):
        raise ConfigurationError(
            "S3 asset storage needs HATCH_S3_BUCKET, HATCH_S3_ACCESS_KEY_ID and "
            "HATCH_S3_SECRET_ACCESS_KEY (plus HATCH_S3_ENDPOINT_URL for R2)."
        )
    return S3AssetStore.connect(
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id.get_secret_value(),
        secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        region=settings.s3_region,
        cache_dir=settings.asset_dir / "s3-cache",
        public_base_url=settings.asset_public_base_url,
    )


def build_media_generator(settings: Settings) -> MediaGenerator:
    if settings.media_provider == "fake":
        # Synthetic media is free, so it must not eat into the real budget.
        return FakeMediaGenerator(cost_per_second_usd=Decimal("0"))
    if settings.higgsfield_api_key is None or settings.higgsfield_api_secret is None:
        raise ConfigurationError(
            "Higgsfield credentials are missing: set HATCH_HIGGSFIELD_API_KEY and "
            "HATCH_HIGGSFIELD_API_SECRET in .env, or use HATCH_MEDIA_PROVIDER=fake."
        )
    return HiggsfieldMediaGenerator(
        api_key=settings.higgsfield_api_key.get_secret_value(),
        api_secret=settings.higgsfield_api_secret.get_secret_value(),
    )


def build_publisher(settings: Settings) -> Publisher:
    if settings.buffer_api_key is None:
        raise ConfigurationError("The Buffer API key is missing: set HATCH_BUFFER_API_KEY in .env.")
    return BufferPublisher(api_key=settings.buffer_api_key.get_secret_value())


def build_posting_schedule(settings: Settings) -> PostingSchedule:
    slots = tuple(
        time.fromisoformat(slot.strip()) for slot in settings.publish_slots_utc.split(",") if slot
    )
    return PostingSchedule(slots_utc=slots)


def build_analytics_adapters(settings: Settings) -> dict[Platform, AnalyticsAdapter]:
    """One official-API adapter per platform that has credentials configured.
    A platform without an adapter records an explicit ingestion failure for
    each observation rather than being silently skipped."""
    tokens = build_token_store(settings)
    stored = tokens.keys()
    adapters: dict[Platform, AnalyticsAdapter] = {}
    if settings.youtube_client_id and settings.youtube_client_secret:
        adapters[Platform.YOUTUBE_SHORTS] = YouTubeAnalytics(
            client_id=settings.youtube_client_id,
            client_secret=settings.youtube_client_secret.get_secret_value(),
            tokens=tokens,
        )
    if any(key.startswith("tiktok_business:") for key in stored):
        # The Business API is the only source of TikTok watch-quality signals.
        adapters[Platform.TIKTOK] = TikTokBusinessAnalytics(tokens=tokens)
    elif settings.tiktok_client_key and settings.tiktok_client_secret:
        adapters[Platform.TIKTOK] = TikTokDisplayAnalytics(
            client_key=settings.tiktok_client_key,
            client_secret=settings.tiktok_client_secret.get_secret_value(),
            tokens=tokens,
        )
    if any(key.startswith("instagram:") for key in stored):
        adapters[Platform.INSTAGRAM_REELS] = InstagramReelsAnalytics(tokens=tokens)
    if any(key.startswith("facebook:") for key in stored):
        adapters[Platform.FACEBOOK_REELS] = FacebookReelsAnalytics(tokens=tokens)
    return adapters


def build_token_store(settings: Settings) -> TokenStore:
    return TokenStore(settings.credentials_file)


def build_language_model(settings: Settings) -> LanguageModel:
    if settings.anthropic_api_key is None:
        raise ConfigurationError(
            "The Anthropic API key is missing: set HATCH_ANTHROPIC_API_KEY in .env."
        )
    return AnthropicLanguageModel(
        client=anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        model=settings.llm_model,
        effort=settings.llm_effort,
    )


def build_qa_gates(settings: Settings) -> GateFactory:
    """Gates every generated video must go through, in order: the free
    technical check first, then the model-based content review (child safety,
    visual, IP/brand, creative).

    Fail-closed: without a configured language model the content gates still
    exist but cannot assess anything, so they escalate every video to a human
    rather than letting it through unchecked."""
    governor = BudgetGovernor(BudgetLimits.from_settings(settings))
    thresholds = ContentThresholds()
    try:
        llm: LanguageModel | None = build_language_model(settings)
    except ConfigurationError:
        llm = None

    def gates(session: Session) -> list[QAGate]:
        if llm is None:
            reviewer = ContentReviewer(
                None, unavailable="content review unavailable: no language model is configured"
            )
        else:
            reviewer = ContentReviewer(bound_caller(session, llm, governor))
        return [TechnicalQAGate(), *content_gates(reviewer, thresholds)]

    return gates


def build_production_deps(settings: Settings) -> ProductionDeps:
    try:
        llm: LanguageModel | None = build_language_model(settings)
    except ConfigurationError:
        llm = None  # single-scene videos do not need one
    return ProductionDeps(
        generator=build_media_generator(settings),
        store=build_asset_store(settings),
        governor=BudgetGovernor(BudgetLimits.from_settings(settings)),
        llm=llm,
        retry=RetryPolicy(
            max_attempts_per_scene=settings.generation_max_attempts_per_scene,
            max_regenerations_after_qa=settings.generation_max_regenerations_after_qa,
        ),
        poll_interval_seconds=settings.generation_poll_interval_seconds,
        timeout_seconds=settings.generation_timeout_seconds,
    )


def build_job_handlers(settings: Settings) -> dict[str, JobHandler]:
    """Every background job type the worker can run."""
    deps = build_production_deps(settings)
    handlers: dict[str, JobHandler] = {
        PRODUCE_SHORT: produce_short_handler(
            deps, poll_interval=timedelta(seconds=settings.generation_poll_interval_seconds)
        ),
        RUN_QA: run_qa_handler(
            gates=build_qa_gates(settings),
            store=deps.store,
            max_regenerations=settings.generation_max_regenerations_after_qa,
        ),
    }
    handlers |= analytics_handlers(build_analytics_adapters(settings))
    handlers |= fitness_handlers(
        HeuristicFitnessEvaluator(), target_cost_usd=settings.budget_target_per_video_usd
    )
    try:
        handlers |= publishing_handlers(publisher=build_publisher(settings), store=deps.store)
    except ConfigurationError as exc:
        logger.warning("publishing_jobs_disabled", extra={"reason": str(exc)})
    try:
        llm = build_language_model(settings)
    except ConfigurationError as exc:
        # Production and QA still run; creative jobs fail visibly as "no handler".
        logger.warning("creative_jobs_disabled", extra={"reason": str(exc)})
    else:
        handlers |= creative_handlers(
            llm=llm,
            governor=deps.governor,
            output=DEFAULT_OUTPUT_REQUIREMENTS,
            production=routed_production(
                # Leave headroom under the per-video target for model calls.
                target_cost_usd=settings.budget_target_per_video_usd,
                max_cost_usd=settings.budget_max_per_generation_usd,
                prompt_strategy=settings.default_prompt_strategy,
                override=settings.video_model_override,
            ),
            policy=AntiCloningPolicy(max_surface_similarity=settings.anti_cloning_max_similarity),
        )
    return handlers
