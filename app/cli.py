"""Hatch command-line interface."""

import uuid
from typing import NoReturn

import typer
from sqlalchemy.orm import Session

from app import __version__
from app.bootstrap import (
    ConfigurationError,
    build_asset_store,
    build_production_deps,
    open_session,
)
from app.config import Settings, get_settings
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.production.models import Asset
from app.production.run import ProductionDeps, ProductionResult, produce_short

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
