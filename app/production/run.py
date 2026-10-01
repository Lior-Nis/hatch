"""Produce one Short for an experiment: the vertical-slice production path.

    experiment → prompt → budget reservation → provider job → stored asset

The run is resumable and idempotent. State is committed before and after every
external side effect, so a crashed or timed-out run can be called again: it
re-attaches to the stored provider job instead of paying for a new one, and a
finished experiment simply returns its existing asset.
"""

import logging
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetExceeded, BudgetGovernor
from app.budgets.models import BudgetLedgerEntry
from app.creative.genome import parse_genes
from app.db import utcnow
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.production.models import Asset, AssetKind, AttemptStatus, GenerationAttempt
from app.production.ports import (
    GenerationJob,
    JobState,
    MediaGenerator,
    MediaOperation,
    MediaRequest,
    ProviderError,
)
from app.production.probe import MediaProbeError, probe_media
from app.production.prompting import build_video_prompt
from app.storage import AssetStore

logger = logging.getLogger(__name__)

_OPEN_ATTEMPT = (AttemptStatus.PENDING, AttemptStatus.RUNNING)
_IN_PRODUCTION = (VideoStatus.STORYBOARDED, VideoStatus.GENERATING)
_FAILED_VIDEO = (
    VideoStatus.GENERATION_FAILED,
    VideoStatus.QA_REJECTED,
    VideoStatus.HUMAN_REJECTED,
    VideoStatus.PUBLISH_FAILED,
    VideoStatus.ABORTED_BUDGET,
)


@dataclass(frozen=True)
class ProductionDeps:
    generator: MediaGenerator
    store: AssetStore
    governor: BudgetGovernor
    poll_interval_seconds: float = 5.0
    timeout_seconds: float = 900.0
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class ProductionResult:
    experiment_id: uuid.UUID
    video_status: VideoStatus
    attempt_id: uuid.UUID | None
    asset_id: uuid.UUID | None
    error: str | None = None


def produce_short(
    session: Session, experiment_id: uuid.UUID, deps: ProductionDeps
) -> ProductionResult:
    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    status = experiment.video_status

    if status is VideoStatus.PROPOSED:
        attempt = _start_attempt(session, experiment, deps)
        if attempt.status is AttemptStatus.BLOCKED_BUDGET:
            return _result(experiment, attempt, error=attempt.error)
    elif status in _IN_PRODUCTION:
        attempt = _open_attempt(session, experiment)
    elif status in _FAILED_VIDEO:
        raise ValueError(f"cannot produce experiment {experiment_id}: video is {status.value}")
    else:
        return _result(experiment, None, asset=_final_video(session, experiment))

    if attempt.provider_job_id is None and not _submit(session, experiment, attempt, deps):
        return _result(experiment, attempt, error=attempt.error)
    return _await_job(session, experiment, attempt, deps)


def _start_attempt(
    session: Session, experiment: Experiment, deps: ProductionDeps
) -> GenerationAttempt:
    genes = parse_genes(experiment.genome.schema_version, experiment.genome.genes)
    prompt = build_video_prompt(
        ip_spec=experiment.ip.spec, genes=genes, creative_spec=experiment.genome.creative_spec
    )
    attempt_number = len(experiment.generation_attempts) + 1
    request = MediaRequest(
        operation=MediaOperation.TEXT_TO_VIDEO,
        model=genes.video_model,
        prompt=prompt,
        duration_seconds=genes.duration_seconds,
        aspect_ratio=genes.aspect_ratio,
        resolution=genes.resolution,
        with_audio=bool(experiment.output_requirements.get("audio_expected", True)),
        idempotency_key=f"{experiment.id}:attempt:{attempt_number}",
    )
    estimate = deps.generator.estimate_cost(request)
    attempt = GenerationAttempt(
        experiment=experiment,
        attempt_number=attempt_number,
        idempotency_key=request.idempotency_key,
        provider=deps.generator.provider,
        model=request.model,
        operation=request.operation.value,
        prompt=prompt,
        request=request.model_dump(mode="json"),
        estimated_cost_usd=estimate.amount_usd,
    )
    session.add(attempt)
    session.flush()
    experiment.status = ExperimentStatus.RUNNING
    try:
        deps.governor.reserve(
            session,
            provider=attempt.provider,
            model=attempt.model,
            operation=attempt.operation,
            estimated_cost_usd=estimate.amount_usd,
            experiment_id=experiment.id,
            generation_attempt_id=attempt.id,
        )
    except BudgetExceeded as exc:
        attempt.status = AttemptStatus.BLOCKED_BUDGET
        attempt.error = str(exc)
        attempt.finished_at = utcnow()
        experiment.video_status = VideoStatus.ABORTED_BUDGET
    else:
        # The creative spec is the script and the single-shot prompt is the
        # storyboard in this slice, so both lifecycle steps are already done.
        experiment.video_status = VideoStatus.SCRIPTED
        experiment.video_status = VideoStatus.STORYBOARDED
    session.commit()
    return attempt


def _open_attempt(session: Session, experiment: Experiment) -> GenerationAttempt:
    attempt = session.scalars(
        select(GenerationAttempt)
        .where(
            GenerationAttempt.experiment_id == experiment.id,
            GenerationAttempt.status.in_(_OPEN_ATTEMPT),
        )
        .order_by(GenerationAttempt.attempt_number.desc())
    ).first()
    if attempt is None:
        raise RuntimeError(f"experiment {experiment.id} is in production with no open attempt")
    return attempt


def _submit(
    session: Session, experiment: Experiment, attempt: GenerationAttempt, deps: ProductionDeps
) -> bool:
    """Start the provider job. Safe to repeat: the provider call is idempotent
    on the attempt's key."""
    request = MediaRequest.model_validate(attempt.request)
    experiment.video_status = VideoStatus.GENERATING
    try:
        job = deps.generator.submit(request)
    except ProviderError as exc:
        _fail(session, experiment, attempt, deps, error=f"submit failed: {exc}", job=None)
        return False
    attempt.provider_job_id = job.provider_job_id
    attempt.status = AttemptStatus.RUNNING
    attempt.started_at = utcnow()
    session.commit()
    return True


def _await_job(
    session: Session, experiment: Experiment, attempt: GenerationAttempt, deps: ProductionDeps
) -> ProductionResult:
    assert attempt.provider_job_id is not None
    deadline = time.monotonic() + deps.timeout_seconds
    while True:
        job = deps.generator.get_job(attempt.provider_job_id)
        if job.state.is_terminal:
            break
        if time.monotonic() >= deadline:
            # Still running at the provider: leave everything resumable.
            return _result(experiment, attempt)
        deps.sleep(deps.poll_interval_seconds)

    if job.state is JobState.FAILED or not job.outputs:
        error = job.error or "provider finished without output"
        _fail(session, experiment, attempt, deps, error=error, job=job)
        return _result(experiment, attempt, error=error)

    try:
        asset = _store_output(session, experiment, attempt, job, deps)
    except (ProviderError, MediaProbeError) as exc:
        error = f"unusable output: {exc}"
        _fail(session, experiment, attempt, deps, error=error, job=job)
        return _result(experiment, attempt, error=error)

    attempt.status = AttemptStatus.SUCCEEDED
    attempt.finished_at = utcnow()
    attempt.actual_cost_usd = job.actual_cost_usd
    attempt.provider_response = dict(job.raw)
    deps.governor.settle(
        session, _reservation(session, attempt), actual_cost_usd=job.actual_cost_usd
    )
    experiment.video_status = VideoStatus.GENERATED
    session.commit()
    return _result(experiment, attempt, asset=asset)


def _store_output(
    session: Session,
    experiment: Experiment,
    attempt: GenerationAttempt,
    job: GenerationJob,
    deps: ProductionDeps,
) -> Asset:
    asset_id = uuid.uuid4()
    with tempfile.TemporaryDirectory(prefix="hatch-") as workdir:
        downloaded = Path(workdir) / "output.mp4"
        deps.generator.download(job.outputs[0], downloaded)
        info = probe_media(downloaded)
        stored = deps.store.put_file(downloaded, f"experiments/{experiment.id}/{asset_id}.mp4")
    asset = Asset(
        id=asset_id,
        experiment=experiment,
        generation_attempt=attempt,
        kind=AssetKind.FINAL_VIDEO,
        storage_uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        mime_type="video/mp4",
        media_info=info.model_dump(mode="json"),
    )
    session.add(asset)
    session.flush()
    return asset


def _fail(
    session: Session,
    experiment: Experiment,
    attempt: GenerationAttempt,
    deps: ProductionDeps,
    *,
    error: str,
    job: GenerationJob | None,
) -> None:
    attempt.status = AttemptStatus.FAILED
    attempt.error = error
    attempt.finished_at = utcnow()
    if job is not None:
        attempt.actual_cost_usd = job.actual_cost_usd
        attempt.provider_response = dict(job.raw)
    # A job that never started cost nothing; otherwise trust the provider's
    # figure, falling back to the estimate when it reports none.
    actual = job.actual_cost_usd if job is not None else Decimal("0")
    deps.governor.settle(session, _reservation(session, attempt), actual_cost_usd=actual)
    experiment.video_status = VideoStatus.GENERATION_FAILED
    logger.warning(
        "generation_failed",
        extra={"experiment_id": str(experiment.id), "attempt_id": str(attempt.id), "error": error},
    )
    session.commit()


def _reservation(session: Session, attempt: GenerationAttempt) -> BudgetLedgerEntry:
    return session.scalars(
        select(BudgetLedgerEntry).where(BudgetLedgerEntry.generation_attempt_id == attempt.id)
    ).one()


def _final_video(session: Session, experiment: Experiment) -> Asset:
    return session.scalars(
        select(Asset)
        .where(Asset.experiment_id == experiment.id, Asset.kind == AssetKind.FINAL_VIDEO)
        .order_by(Asset.created_at.desc())
    ).one()


def _result(
    experiment: Experiment,
    attempt: GenerationAttempt | None,
    *,
    asset: Asset | None = None,
    error: str | None = None,
) -> ProductionResult:
    return ProductionResult(
        experiment_id=experiment.id,
        video_status=experiment.video_status,
        attempt_id=attempt.id if attempt else None,
        asset_id=asset.id if asset else None,
        error=error,
    )
