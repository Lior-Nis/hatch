"""Hatch command-line interface."""

import os
import socket
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Annotated, NoReturn

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.bootstrap import (
    ConfigurationError,
    build_asset_store,
    build_job_handlers,
    build_production_deps,
    build_qa_gates,
    open_session,
)
from app.budgets.reports import spend_report
from app.config import Settings, get_settings
from app.db import make_engine, utcnow
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.lineage import ExperimentNotFound, get_lineage
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.experiments.states import VideoStatus
from app.ips.catalog import INITIAL_IPS, seed_initial_ips
from app.observability.health import find_stalls, provider_health
from app.observability.logging import configure_logging
from app.observability.trace import experiment_timeline, render_timeline
from app.production.catalog import seed_provider_models
from app.production.jobs import enqueue_production
from app.production.models import Asset
from app.production.routing import load_catalog
from app.production.run import ProductionDeps, ProductionResult, produce_short
from app.quality.runner import run_quality_gates
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.worker import Worker

app = typer.Typer(help="Hatch — evolutionary kids' media studio.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Hatch — evolutionary kids' media studio."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_format=settings.log_format == "json")


@app.command()
def info() -> None:
    """Show the running version, environment, and budget limits."""
    settings = get_settings()
    typer.echo(f"hatch {__version__}")
    typer.echo(f"environment: {settings.env}")
    typer.echo(f"media provider: {settings.media_provider}")
    typer.echo("budget limits (USD):")
    typer.echo(f"  target per video: ${settings.budget_target_per_video_usd:.2f}")
    typer.echo(f"  max per generation: ${settings.budget_max_per_generation_usd:.2f}")
    typer.echo(f"  max per video: ${settings.budget_max_per_video_usd:.2f}")
    typer.echo(f"  daily ceiling: ${settings.budget_daily_usd:.2f}")
    typer.echo(f"  monthly ceiling: ${settings.budget_monthly_usd:.2f}")


@app.command("seed-ips")
def seed_ips() -> None:
    """Create the three initial IPs with their characters and starting hypotheses."""
    with open_session(get_settings()) as session:
        seed_initial_ips(session)
        session.commit()
        for definition in INITIAL_IPS:
            typer.echo(f"{definition.ip.slug}: {definition.ip.name} ({definition.ip.category})")


@app.command("seed-models")
def seed_models() -> None:
    """Add missing video models to the routing catalogue (never overwrites edits)."""
    with open_session(get_settings()) as session:
        seed_provider_models(session)
        session.commit()
        for spec in load_catalog(session):
            prices = ", ".join(f"{r} ${p}/s" for r, p in spec.usd_per_second.items())
            typer.echo(f"{spec.provider} {spec.model}: {prices}")


@app.command("run-fixture")
def run_fixture(
    background: Annotated[
        bool,
        typer.Option("--background", help="Queue the work for `hatch worker` instead of running."),
    ] = False,
) -> None:
    """Create the hard-coded vertical-slice experiment and produce its Short."""
    settings = get_settings()
    deps = _production_deps(settings)  # fail before creating anything
    with open_session(settings) as session:
        experiment = create_experiment(session, FIRST_SHORT)
        session.commit()
        typer.echo(f"experiment: {experiment.id}")
        if background:
            job = enqueue_production(session, experiment.id)
            session.commit()
            typer.echo(f"queued job: {job.id} (run `hatch worker` to process it)")
        else:
            _produce(session, settings, deps, experiment.id)


@app.command()
def worker(
    until_idle: Annotated[
        bool,
        typer.Option("--until-idle", help="Exit when no job is due instead of waiting for more."),
    ] = False,
) -> None:
    """Run background jobs (generation, QA, ...) from the durable queue."""
    settings = get_settings()
    try:
        handlers = build_job_handlers(settings)
    except ConfigurationError as exc:
        _fail(str(exc))
    engine = make_engine(settings.database_url)
    job_worker = Worker(
        lambda: Session(engine, expire_on_commit=False),
        handlers,
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
    )
    try:
        if until_idle:
            while job_worker.run_once():
                pass
        else:
            typer.echo("worker started; press Ctrl+C to stop")
            job_worker.run_forever()
    except KeyboardInterrupt:
        typer.echo("worker stopped")
    finally:
        engine.dispose()


@app.command()
def jobs(
    status: Annotated[JobStatus | None, typer.Option(help="Only show jobs in this status.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum number of jobs to show.")] = 30,
) -> None:
    """List background jobs, newest first: status, attempts, timing, experiment."""
    with open_session(get_settings()) as session:
        query = select(JobRun).order_by(JobRun.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(JobRun.status == status)
        for job in session.scalars(query):
            finished = job.finished_at.strftime("%Y-%m-%d %H:%M:%S") if job.finished_at else "-"
            typer.echo(
                f"{job.status.value:<10} {job.job_type:<16} attempts={job.attempts}/"
                f"{job.max_attempts} experiment={job.experiment_id or '-'} "
                f"finished={finished} id={job.id}"
            )
            if job.error:
                typer.echo(f"           error: {job.error}")


@app.command()
def produce(experiment_id: uuid.UUID) -> None:
    """Produce (or resume producing) the Short for an existing experiment."""
    settings = get_settings()
    deps = _production_deps(settings)
    with open_session(settings) as session:
        if session.get(Experiment, experiment_id) is None:
            _fail(f"no experiment with id {experiment_id}")
        _produce(session, settings, deps, experiment_id)


@app.command()
def lineage(experiment_id: uuid.UUID) -> None:
    """Print, as JSON, why an experiment's video exists and how it was made."""
    with open_session(get_settings()) as session:
        try:
            report = get_lineage(session, experiment_id)
        except ExperimentNotFound:
            _fail(f"no experiment with id {experiment_id}")
        typer.echo(report.model_dump_json(indent=2))


@app.command()
def costs(days: Annotated[int, typer.Option(help="Report window in days.")] = 30) -> None:
    """Print, as JSON, spend by video, IP, provider and day."""
    since = utcnow() - timedelta(days=days)
    with open_session(get_settings()) as session:
        typer.echo(spend_report(session, since=since).model_dump_json(indent=2))


@app.command()
def trace(experiment_id: uuid.UUID) -> None:
    """Show everything that happened to an experiment, in order."""
    with open_session(get_settings()) as session:
        try:
            typer.echo(render_timeline(experiment_timeline(session, experiment_id)))
        except ExperimentNotFound:
            _fail(f"no experiment with id {experiment_id}")


@app.command()
def health() -> None:
    """Provider reliability, failed jobs and stalled work. Exits 1 on problems."""
    with open_session(get_settings()) as session:
        typer.echo("providers:")
        for provider in provider_health(session):
            rate = f"{provider.failure_rate:.0%}" if provider.failure_rate is not None else "n/a"
            typer.echo(
                f"  {provider.provider} {provider.model}: {provider.succeeded} ok, "
                f"{provider.failed} failed ({rate}), {provider.in_flight} in flight, "
                f"{provider.blocked_by_budget} blocked by budget"
            )
            if provider.last_error:
                typer.echo(f"    last error: {provider.last_error}")
        failed_jobs = session.scalars(
            select(JobRun).where(JobRun.status == JobStatus.FAILED).order_by(JobRun.created_at)
        ).all()
        typer.echo(f"failed jobs: {len(failed_jobs)}")
        for job in failed_jobs:
            typer.echo(f"  {job.job_type} {job.id} experiment={job.experiment_id}: {job.error}")
        stalls = find_stalls(session)
        typer.echo(f"stalls: {len(stalls)}")
        for stall in stalls:
            subject = stall.experiment_id or stall.job_id
            typer.echo(
                f"  {stall.kind} {subject} since {stall.since:%Y-%m-%d %H:%M}: {stall.detail}"
            )
        if failed_jobs or stalls:
            raise typer.Exit(code=1)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8321) -> None:
    """Run the operator web UI (human review). Local-only: it has no login."""
    import uvicorn

    from app.admin.web import create_app

    uvicorn.run(create_app(), host=host, port=port)


def _production_deps(settings: Settings) -> ProductionDeps:
    try:
        return build_production_deps(settings)
    except ConfigurationError as exc:
        _fail(str(exc))


def _produce(
    session: Session, settings: Settings, deps: ProductionDeps, experiment_id: uuid.UUID
) -> None:
    try:
        result = produce_short(session, experiment_id, deps)
    except ValueError as exc:
        _fail(str(exc))
    if result.video_status is VideoStatus.GENERATED:
        report = run_quality_gates(
            session, experiment_id, gates=build_qa_gates(settings)(session), store=deps.store
        )
        for verdict in report.verdicts:
            typer.echo(f"qa {verdict.gate}: {verdict.outcome.value}")
            for reason in verdict.reasons:
                typer.echo(f"  - {reason}")
        result = replace(result, video_status=report.video_status)
    _report(session, settings, result)


def _report(session: Session, settings: Settings, result: ProductionResult) -> None:
    typer.echo(f"video status: {result.video_status.value}")
    if result.error:
        typer.echo(f"error: {result.error}")
    if result.asset_id is not None:
        asset = session.get_one(Asset, result.asset_id)
        typer.echo(f"asset: {asset.id}")
        typer.echo(f"file: {build_asset_store(settings).local_path(asset.storage_uri)}")
    elif result.error is None:
        typer.echo(f"still generating; resume with: hatch produce {result.experiment_id}")
    if result.error:
        raise typer.Exit(code=1)


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)
