"""Hatch command-line interface."""

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import NoReturn

import typer
from sqlalchemy.orm import Session

from app import __version__
from app.bootstrap import (
    ConfigurationError,
    build_asset_store,
    build_production_deps,
    build_qa_gates,
    open_session,
)
from app.budgets.reports import spend_report
from app.config import Settings, get_settings
from app.db import utcnow
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.lineage import ExperimentNotFound, get_lineage
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.experiments.states import VideoStatus
from app.production.models import Asset
from app.production.run import ProductionDeps, ProductionResult, produce_short
from app.quality.runner import run_quality_gates

app = typer.Typer(help="Hatch — evolutionary kids' media studio.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Hatch — evolutionary kids' media studio."""


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


@app.command("run-fixture")
def run_fixture() -> None:
    """Create the hard-coded vertical-slice experiment and produce its Short."""
    settings = get_settings()
    deps = _production_deps(settings)  # fail before creating anything
    with open_session(settings) as session:
        experiment = create_experiment(session, FIRST_SHORT)
        session.commit()
        typer.echo(f"experiment: {experiment.id}")
        _produce(session, settings, deps, experiment.id)


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
def costs(days: int = typer.Option(30, help="Report window in days.")) -> None:
    """Print, as JSON, spend by video, IP, provider and day."""
    since = utcnow() - timedelta(days=days)
    with open_session(get_settings()) as session:
        typer.echo(spend_report(session, since=since).model_dump_json(indent=2))


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
            session, experiment_id, gates=build_qa_gates(settings), store=deps.store
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
