"""Hatch command-line interface."""

import typer

from app import __version__
from app.config import get_settings

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
    typer.echo("budget limits (USD):")
    typer.echo(f"  target per video: ${settings.budget_target_per_video_usd:.2f}")
    typer.echo(f"  max per video: ${settings.budget_max_per_video_usd:.2f}")
    typer.echo(f"  daily ceiling: ${settings.budget_daily_usd:.2f}")
    typer.echo(f"  monthly ceiling: ${settings.budget_monthly_usd:.2f}")
