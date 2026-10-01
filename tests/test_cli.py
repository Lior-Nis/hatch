from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()


def test_info_command_reports_version_and_budget_limits() -> None:
    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0
    assert "hatch 0.1.0" in result.output
    assert "max per video: $1.50" in result.output
