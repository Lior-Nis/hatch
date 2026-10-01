import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app
from app.config import get_settings
from app.db import Base
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.probe import probe_media
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()


@pytest.fixture
def cli_env(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at the test database and a temp asset dir. The CLI commits
    for real, so every table is emptied afterwards."""
    monkeypatch.setenv("HATCH_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("HATCH_MEDIA_PROVIDER", "fake")
    monkeypatch.setenv("HATCH_ASSET_DIR", str(tmp_path / "assets"))
    get_settings.cache_clear()
    yield tmp_path / "assets"
    get_settings.cache_clear()
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))


def test_run_fixture_produces_a_playable_short_without_manual_edits(
    cli_env: Path, engine: Engine
) -> None:
    result = runner.invoke(app, ["run-fixture"])

    assert result.exit_code == 0, result.output
    assert "video status: generated" in result.output
    with Session(engine) as session:
        experiment = session.scalars(select(Experiment)).one()
        assert experiment.video_status is VideoStatus.GENERATED
        [asset] = experiment.assets
    [stored] = list(cli_env.rglob("*.mp4"))
    assert stored.stat().st_size == asset.size_bytes
    assert probe_media(stored).height > probe_media(stored).width
    assert str(stored) in result.output


def test_produce_resumes_an_existing_experiment_by_id(cli_env: Path, engine: Engine) -> None:
    runner.invoke(app, ["run-fixture"])
    with Session(engine) as session:
        experiment_id = session.scalars(select(Experiment.id)).one()

    result = runner.invoke(app, ["produce", str(experiment_id)])

    assert result.exit_code == 0, result.output
    assert "video status: generated" in result.output
    assert len(list(cli_env.rglob("*.mp4"))) == 1


def test_run_fixture_fails_cleanly_when_higgsfield_credentials_are_missing(
    cli_env: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HATCH_MEDIA_PROVIDER", "higgsfield")
    monkeypatch.delenv("HATCH_HIGGSFIELD_API_KEY", raising=False)
    get_settings.cache_clear()

    result = runner.invoke(app, ["run-fixture"])

    assert result.exit_code != 0
    assert "HATCH_HIGGSFIELD_API_KEY" in result.output
    with Session(engine) as session:
        assert session.scalars(select(Experiment)).all() == []


def test_run_fixture_works_in_a_fresh_interpreter(cli_env: Path) -> None:
    """Guards against ORM models that are only registered because some other
    test module happened to import them."""
    completed = subprocess.run(
        [sys.executable, "-c", "from app.cli import app; app()", "run-fixture"],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "video status: generated" in completed.stdout
