"""Production orchestrator: turn one experiment into one finished Short.

The work is a small deterministic graph derived from the genome:

    script → storyboard → scene:1 … scene:N → assemble

Each node is a persisted ``ProductionStep``. Scenes are generated one at a
time through the ``MediaGenerator`` port, each attempt reserved against the
budget first. A failed attempt is retried — repaired prompt, then a fallback
model — until it succeeds, the retry policy is exhausted, or the budget
governor refuses. Every attempt is recorded.

The run is resumable and idempotent: state is committed before and after
every external side effect, so calling it again re-attaches to a stored
provider job instead of paying for a new one, and a finished experiment simply
returns its existing asset.
"""

import hashlib
import logging
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetExceeded, BudgetGovernor
from app.budgets.models import BudgetLedgerEntry
from app.creative.genome import Genes, parse_genes
from app.db import utcnow
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.llm.ports import LanguageModel, LLMError
from app.production.assemble import concat_videos
from app.production.models import (
    Asset,
    AssetKind,
    AttemptStatus,
    GenerationAttempt,
    ProductionStep,
    StepStatus,
)
from app.production.ports import (
    GenerationJob,
    JobState,
    MediaGenerator,
    MediaOperation,
    MediaRequest,
    ProductionError,
    ProviderError,
)
from app.production.probe import MediaProbeError, probe_media
from app.production.prompting import build_video_prompt, repair_prompt
from app.production.storyboard import Scene, build_storyboard
from app.quality.models import QAResult
from app.quality.ports import QAOutcome
from app.storage import AssetStore

__all__ = [
    "ProductionDeps",
    "ProductionError",
    "ProductionResult",
    "RetryPolicy",
    "plan_steps",
    "produce_short",
]

logger = logging.getLogger(__name__)

_OPEN_ATTEMPT = (AttemptStatus.PENDING, AttemptStatus.RUNNING)
_PRODUCIBLE = (
    VideoStatus.PROPOSED,
    VideoStatus.SCRIPTED,
    VideoStatus.STORYBOARDED,
    VideoStatus.GENERATING,
)
_DEAD_END = (
    VideoStatus.GENERATION_FAILED,
    VideoStatus.HUMAN_REJECTED,
    VideoStatus.PUBLISH_FAILED,
    VideoStatus.ABORTED_BUDGET,
)
# Attempt strategies, in order, for a first production and for a regeneration
# after automated QA rejected the video.
_FIRST_CYCLE: tuple[str, ...] = ("original", "prompt_repair", "fallback_model")
_QA_CYCLE: tuple[str, ...] = ("qa_repair", "fallback_model")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_scene: int = 3
    """Attempts per scene before the video is GENERATION_FAILED."""
    max_regenerations_after_qa: int = 1
    """How many times a video rejected by automated QA is generated again."""


@dataclass(frozen=True)
class ProductionDeps:
    generator: MediaGenerator
    store: AssetStore
    governor: BudgetGovernor
    llm: LanguageModel | None = None
    """Only needed to storyboard genomes with more than one scene."""
    retry: RetryPolicy = field(default_factory=RetryPolicy)
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


def plan_steps(genes: Genes) -> list[tuple[str, str]]:
    """The production graph for a genome, as ordered ``(key, kind)`` pairs."""
    scenes = [(f"scene:{index}", "scene") for index in range(1, genes.scene_count + 1)]
    return [("script", "script"), ("storyboard", "storyboard"), *scenes, ("assemble", "assemble")]


def produce_short(
    session: Session, experiment_id: uuid.UUID, deps: ProductionDeps
) -> ProductionResult:
    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    status = experiment.video_status
    if status in _DEAD_END:
        raise ValueError(f"cannot produce experiment {experiment_id}: video is {status.value}")
    genes = parse_genes(experiment.genome.schema_version, experiment.genome.genes)
    steps = _ensure_steps(session, experiment, genes)
    if status is VideoStatus.QA_REJECTED:
        _begin_regeneration(session, experiment, steps, deps)
    elif status not in _PRODUCIBLE:
        return _result(experiment, None, asset=_final_video(session, experiment))
    if experiment.status is ExperimentStatus.CANDIDATE_CREATED:
        experiment.status = ExperimentStatus.RUNNING

    runners = {
        "script": _run_script,
        "storyboard": _run_storyboard,
        "scene": _run_scene,
        "assemble": _run_assemble,
    }
    for step in steps:
        if step.status is StepStatus.SUCCEEDED:
            continue
        stopped = runners[step.kind](session, experiment, genes, step, steps, deps)
        if stopped is not None:
            return stopped
    return _result(
        experiment, _latest_attempt(session, experiment), asset=_final_video(session, experiment)
    )


# --- graph bookkeeping ---------------------------------------------------------


def _ensure_steps(session: Session, experiment: Experiment, genes: Genes) -> list[ProductionStep]:
    existing = {
        step.key: step
        for step in session.scalars(
            select(ProductionStep).where(ProductionStep.experiment_id == experiment.id)
        )
    }
    steps = []
    for position, (key, kind) in enumerate(plan_steps(genes)):
        step = existing.get(key)
        if step is None:
            step = ProductionStep(experiment=experiment, key=key, kind=kind, position=position)
            session.add(step)
        steps.append(step)
    session.flush()
    return steps


def _start(step: ProductionStep) -> None:
    if step.status is not StepStatus.RUNNING:
        step.status = StepStatus.RUNNING
        step.started_at = step.started_at or utcnow()
        step.error = None


def _succeed(step: ProductionStep, **detail: object) -> None:
    step.status = StepStatus.SUCCEEDED
    step.detail = {**step.detail, **detail}
    step.finished_at = utcnow()


def _fail_step(step: ProductionStep, error: str) -> None:
    step.status = StepStatus.FAILED
    step.error = error
    step.finished_at = utcnow()


def _begin_regeneration(
    session: Session, experiment: Experiment, steps: list[ProductionStep], deps: ProductionDeps
) -> None:
    """Automated QA rejected the video: generate it again, telling the model
    what was wrong. The rejected asset stays rejected; the new one faces QA
    from the start."""
    done = sum(1 for a in experiment.generation_attempts if a.strategy == "qa_repair")
    scenes = [step for step in steps if step.kind == "scene"]
    if done >= deps.retry.max_regenerations_after_qa * len(scenes):
        raise ValueError(
            f"cannot produce experiment {experiment.id}: video is qa_rejected and the "
            "regeneration limit is reached"
        )
    rejected = _final_video(session, experiment)
    reasons = [
        reason
        for result in session.scalars(
            select(QAResult).where(
                QAResult.asset_id == rejected.id,
                QAResult.mandatory.is_(True),
                QAResult.outcome == QAOutcome.FAIL,
            )
        )
        for reason in result.reasons or [f"{result.gate} check failed"]
    ]
    last_attempt = max((a.attempt_number for a in experiment.generation_attempts), default=0)
    for step in steps:
        if step.kind in ("scene", "assemble"):
            step.status = StepStatus.PENDING
            step.finished_at = None
            step.detail = {
                **step.detail,
                "qa_feedback": "; ".join(reasons),
                "cycle_started_after_attempt": last_attempt,
            }
    experiment.video_status = VideoStatus.GENERATING
    session.commit()


# --- steps ---------------------------------------------------------------------


def _run_script(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    step: ProductionStep,
    steps: list[ProductionStep],
    deps: ProductionDeps,
) -> ProductionResult | None:
    """The creative spec is the script: record exactly which text was produced."""
    _start(step)
    spec = experiment.genome.creative_spec
    _succeed(step, creative_spec_sha256=hashlib.sha256(spec.encode()).hexdigest())
    if experiment.video_status is VideoStatus.PROPOSED:
        experiment.video_status = VideoStatus.SCRIPTED
    session.commit()
    return None


def _run_storyboard(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    step: ProductionStep,
    steps: list[ProductionStep],
    deps: ProductionDeps,
) -> ProductionResult | None:
    _start(step)
    try:
        board, source = build_storyboard(
            session, experiment, genes, llm=deps.llm, governor=deps.governor
        )
    except (ProductionError, LLMError, BudgetExceeded) as exc:
        _fail_step(step, str(exc))
        session.commit()
        if isinstance(exc, ProductionError):
            raise
        raise ProductionError(f"storyboard failed: {exc}") from exc
    _succeed(step, scenes=[scene.model_dump() for scene in board.scenes], source=source)
    if experiment.video_status is VideoStatus.SCRIPTED:
        experiment.video_status = VideoStatus.STORYBOARDED
    session.commit()
    return None


def _run_scene(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    step: ProductionStep,
    steps: list[ProductionStep],
    deps: ProductionDeps,
) -> ProductionResult | None:
    storyboard = next(s for s in steps if s.kind == "storyboard").detail["scenes"]
    scene = next(Scene.model_validate(s) for s in storyboard if f"scene:{s['index']}" == step.key)
    base_prompt = build_video_prompt(
        ip_spec=experiment.ip.spec,
        genes=genes,
        creative_spec=scene.description,
        duration_seconds=scene.duration_seconds,
        scene=(scene.index, len(storyboard)),
    )
    _start(step)
    step.detail = {**step.detail, "prompt": base_prompt}

    while True:
        mine = [a for a in _attempts(session, experiment) if a.step_id == step.id]
        current = next((a for a in reversed(mine) if a.status in _OPEN_ATTEMPT), None)
        if current is None:
            current = _next_attempt(
                session, experiment, genes, step, scene, base_prompt, mine, deps
            )
            if current is None:  # retries exhausted
                error = mine[-1].error if mine else "no attempt could be made"
                _fail_step(step, error or "generation failed")
                experiment.video_status = VideoStatus.GENERATION_FAILED
                session.commit()
                return _result(experiment, mine[-1] if mine else None, error=error)
            if current.status is AttemptStatus.BLOCKED_BUDGET:
                _fail_step(step, current.error or "blocked by budget")
                experiment.video_status = VideoStatus.ABORTED_BUDGET
                session.commit()
                return _result(experiment, current, error=current.error)
            if current.status is AttemptStatus.FAILED:  # could not even be priced
                continue

        if current.provider_job_id is None and not _submit(session, experiment, current, deps):
            continue  # submission failed: move on to the next strategy
        job = _poll(current, deps)
        if job is None:
            return _result(experiment, current)  # still rendering: resumable
        if job.state is JobState.SUCCEEDED and job.outputs:
            try:
                asset = _store_clip(session, experiment, current, job, deps)
            except (ProviderError, MediaProbeError) as exc:
                _close_attempt(session, current, deps, job=job, error=f"unusable output: {exc}")
                continue
            _close_attempt(session, current, deps, job=job, error=None)
            _succeed(step, asset_id=str(asset.id), attempt_id=str(current.id))
            session.commit()
            return None
        _close_attempt(
            session,
            current,
            deps,
            job=job,
            error=job.error or "provider finished without output",
        )


def _run_assemble(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    step: ProductionStep,
    steps: list[ProductionStep],
    deps: ProductionDeps,
) -> ProductionResult | None:
    _start(step)
    clips = [
        session.get_one(Asset, uuid.UUID(scene.detail["asset_id"]))
        for scene in steps
        if scene.kind == "scene"
    ]
    if len(clips) == 1:
        # One scene is already the finished video: same stored object, new role.
        clip = clips[0]
        final = Asset(
            experiment=experiment,
            generation_attempt_id=clip.generation_attempt_id,
            kind=AssetKind.FINAL_VIDEO,
            storage_uri=clip.storage_uri,
            sha256=clip.sha256,
            size_bytes=clip.size_bytes,
            mime_type=clip.mime_type,
            media_info=clip.media_info,
        )
    else:
        asset_id = uuid.uuid4()
        with tempfile.TemporaryDirectory(prefix="hatch-") as workdir:
            assembled = Path(workdir) / "final.mp4"
            try:
                concat_videos([deps.store.local_path(c.storage_uri) for c in clips], assembled)
                info = probe_media(assembled)
            except (ProductionError, MediaProbeError) as exc:
                _fail_step(step, str(exc))
                experiment.video_status = VideoStatus.GENERATION_FAILED
                session.commit()
                return _result(experiment, None, error=str(exc))
            stored = deps.store.put_file(assembled, f"experiments/{experiment.id}/{asset_id}.mp4")
        final = Asset(
            id=asset_id,
            experiment=experiment,
            kind=AssetKind.FINAL_VIDEO,
            storage_uri=stored.uri,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            mime_type="video/mp4",
            media_info=info.model_dump(mode="json"),
        )
    session.add(final)
    session.flush()
    _succeed(step, final_asset_id=str(final.id), clips=[str(c.id) for c in clips])
    experiment.video_status = VideoStatus.GENERATED
    session.commit()
    return None


# --- attempts --------------------------------------------------------------------


def _attempts(session: Session, experiment: Experiment) -> list[GenerationAttempt]:
    return list(
        session.scalars(
            select(GenerationAttempt)
            .where(GenerationAttempt.experiment_id == experiment.id)
            .order_by(GenerationAttempt.attempt_number)
        )
    )


def _latest_attempt(session: Session, experiment: Experiment) -> GenerationAttempt | None:
    attempts = _attempts(session, experiment)
    return attempts[-1] if attempts else None


def _next_attempt(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    step: ProductionStep,
    scene: Scene,
    base_prompt: str,
    mine: list[GenerationAttempt],
    deps: ProductionDeps,
) -> GenerationAttempt | None:
    """Create the next attempt for a scene according to the retry policy, or
    return None when the policy is exhausted."""
    cycle_start = int(step.detail.get("cycle_started_after_attempt", 0))
    in_cycle = [a for a in mine if a.attempt_number > cycle_start]
    sequence = _QA_CYCLE if cycle_start else _FIRST_CYCLE
    sequence = sequence[: deps.retry.max_attempts_per_scene]
    if len(in_cycle) >= len(sequence):
        return None
    strategy = sequence[len(in_cycle)]
    last_error = in_cycle[-1].error if in_cycle else None

    model, resolution = genes.video_model, genes.resolution
    if strategy == "fallback_model":
        tried = {(a.model, a.request.get("resolution")) for a in mine}
        fallbacks = (experiment.production_plan.get("routing") or {}).get("fallbacks") or []
        untried = [f for f in fallbacks if (f["model"], f["resolution"]) not in tried]
        if untried:
            model, resolution = untried[0]["model"], untried[0]["resolution"]
        else:
            strategy = "prompt_repair"  # nothing to fall back to: repair again
    if strategy == "original":
        prompt = base_prompt
    elif strategy == "qa_repair":
        prompt = repair_prompt(base_prompt, step.detail.get("qa_feedback"))
    else:
        prompt = repair_prompt(base_prompt, last_error)

    attempt_number = len(_attempts(session, experiment)) + 1
    request = MediaRequest(
        operation=MediaOperation.TEXT_TO_VIDEO,
        model=model,
        prompt=prompt,
        duration_seconds=scene.duration_seconds,
        aspect_ratio=genes.aspect_ratio,
        resolution=None if resolution == "default" else resolution,
        with_audio=bool(experiment.output_requirements.get("audio_expected", True)),
        idempotency_key=f"{experiment.id}:attempt:{attempt_number}",
    )
    attempt = GenerationAttempt(
        experiment=experiment,
        step_id=step.id,
        strategy=strategy,
        attempt_number=attempt_number,
        idempotency_key=request.idempotency_key,
        provider=deps.generator.provider,
        model=request.model,
        operation=request.operation.value,
        prompt=prompt,
        request=request.model_dump(mode="json"),
    )
    try:
        estimate = deps.generator.estimate_cost(request)
    except ProviderError as exc:
        attempt.status = AttemptStatus.FAILED
        attempt.error = f"could not price the request: {exc}"
        attempt.finished_at = utcnow()
        session.add(attempt)
        session.commit()
        return attempt
    attempt.estimated_cost_usd = estimate.amount_usd
    session.add(attempt)
    session.flush()
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
    session.commit()
    return attempt


def _submit(
    session: Session, experiment: Experiment, attempt: GenerationAttempt, deps: ProductionDeps
) -> bool:
    """Start the provider job. Safe to repeat: the provider call is idempotent
    on the attempt's key."""
    request = MediaRequest.model_validate(attempt.request)
    if experiment.video_status is not VideoStatus.GENERATING:
        experiment.video_status = VideoStatus.GENERATING
    try:
        job = deps.generator.submit(request)
    except ProviderError as exc:
        _close_attempt(session, attempt, deps, job=None, error=f"submit failed: {exc}")
        return False
    attempt.provider_job_id = job.provider_job_id
    attempt.status = AttemptStatus.RUNNING
    attempt.started_at = utcnow()
    session.commit()
    return True


def _poll(attempt: GenerationAttempt, deps: ProductionDeps) -> GenerationJob | None:
    """Wait for the job to finish. None means it is still running at timeout."""
    assert attempt.provider_job_id is not None
    deadline = time.monotonic() + deps.timeout_seconds
    while True:
        job = deps.generator.get_job(attempt.provider_job_id)
        if job.state.is_terminal:
            return job
        if time.monotonic() >= deadline:
            return None
        deps.sleep(deps.poll_interval_seconds)


def _store_clip(
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
        kind=AssetKind.RAW_VIDEO,
        storage_uri=stored.uri,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        mime_type="video/mp4",
        media_info=info.model_dump(mode="json"),
    )
    session.add(asset)
    session.flush()
    return asset


def _close_attempt(
    session: Session,
    attempt: GenerationAttempt,
    deps: ProductionDeps,
    *,
    job: GenerationJob | None,
    error: str | None,
) -> None:
    """Finish an attempt (success when ``error`` is None) and settle its
    reservation."""
    attempt.status = AttemptStatus.FAILED if error else AttemptStatus.SUCCEEDED
    attempt.error = error
    attempt.finished_at = utcnow()
    # A job that never started cost nothing; otherwise trust the provider's
    # figure, and keep counting the estimate when it reports none.
    actual = job.actual_cost_usd if job is not None else Decimal("0")
    if job is not None:
        attempt.actual_cost_usd = job.actual_cost_usd
        attempt.provider_response = dict(job.raw)
    reservation = session.scalars(
        select(BudgetLedgerEntry).where(BudgetLedgerEntry.generation_attempt_id == attempt.id)
    ).one()
    deps.governor.settle(session, reservation, actual_cost_usd=actual)
    if error:
        logger.warning(
            "generation_failed",
            extra={
                "experiment_id": str(attempt.experiment_id),
                "attempt_id": str(attempt.id),
                "strategy": attempt.strategy,
                "error": error,
            },
        )
    session.commit()


# --- results -----------------------------------------------------------------------


def _final_video(session: Session, experiment: Experiment) -> Asset:
    return session.scalars(
        select(Asset)
        .where(Asset.experiment_id == experiment.id, Asset.kind == AssetKind.FINAL_VIDEO)
        .order_by(Asset.created_at.desc())
    ).first() or _missing_final(experiment)


def _missing_final(experiment: Experiment) -> Asset:
    raise RuntimeError(f"experiment {experiment.id} has no final video asset")


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
