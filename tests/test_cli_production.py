import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app
from app.config import get_settings
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.production.probe import probe_media
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()


@pytest.fixture
def cli_env(
    committed_db: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point the CLI at the test database and a temp asset dir."""
    monkeypatch.setenv("HATCH_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("HATCH_MEDIA_PROVIDER", "fake")
    monkeypatch.setenv("HATCH_ASSET_DIR", str(tmp_path / "assets"))
    get_settings.cache_clear()
    yield tmp_path / "assets"
    get_settings.cache_clear()


def test_run_fixture_produces_a_playable_short_without_manual_edits(
    cli_env: Path, engine: Engine
) -> None:
    result = runner.invoke(app, ["run-fixture"])

    assert result.exit_code == 0, result.output
    assert "video status: approval_pending" in result.output
    with Session(engine) as session:
        experiment = session.scalars(select(Experiment)).one()
        assert experiment.video_status is VideoStatus.APPROVAL_PENDING
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
    assert "video status: approval_pending" in result.output
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
    assert "video status: approval_pending" in completed.stdout


def test_lineage_command_prints_the_full_causal_record_as_json(
    cli_env: Path, engine: Engine
) -> None:
    runner.invoke(app, ["run-fixture"])
    with Session(engine) as session:
        experiment_id = session.scalars(select(Experiment.id)).one()

    result = runner.invoke(app, ["lineage", str(experiment_id)])

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["hypothesis"]["source"] == "fixture"
    assert document["generation_attempts"][0]["status"] == "succeeded"
    assert document["final_asset"]["mime_type"] == "video/mp4"


def test_background_run_is_processed_by_the_worker_and_listed(
    cli_env: Path, committed_db: Engine
) -> None:
    queued = runner.invoke(app, ["run-fixture", "--background"])
    assert queued.exit_code == 0, queued.output
    assert "queued job" in queued.output
    with Session(committed_db) as session:
        assert session.scalars(select(Experiment)).one().video_status is VideoStatus.PROPOSED

    worked = runner.invoke(app, ["worker", "--until-idle"])
    listing = runner.invoke(app, ["jobs"])

    assert worked.exit_code == 0, worked.output
    with Session(committed_db) as session:
        experiment = session.scalars(select(Experiment)).one()
        assert experiment.video_status is VideoStatus.APPROVAL_PENDING
    assert listing.exit_code == 0, listing.output
    assert listing.output.count("succeeded") == 2
    assert "produce_short" in listing.output and "run_qa" in listing.output
    assert str(experiment.id) in listing.output
