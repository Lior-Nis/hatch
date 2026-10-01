from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.cli import app
from app.config import get_settings
from app.platforms import Platform
from app.publishing.models import PlatformAccount
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()


@pytest.fixture
def cli_db(committed_db: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.setenv("HATCH_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("HATCH_BUFFER_API_KEY", raising=False)
    get_settings.cache_clear()
    runner.invoke(app, ["seed-ips"])
    yield committed_db
    get_settings.cache_clear()


def test_an_account_is_mapped_to_an_ip_and_listed(cli_db: Engine) -> None:
    mapped = runner.invoke(
        app,
        ["accounts", "map", "nibbin-hollow", "youtube_shorts", "--channel-id", "buf-yt",
         "--external-account-id", "UC123", "--handle", "@nibbinhollow"],
    )  # fmt: skip
    listed = runner.invoke(app, ["accounts", "list"])

    assert mapped.exit_code == 0, mapped.output
    with Session(cli_db) as session:
        account = session.scalars(select(PlatformAccount)).one()
        assert account.platform is Platform.YOUTUBE_SHORTS
        assert (account.publisher_profile_id, account.external_account_id) == ("buf-yt", "UC123")
        assert account.ip.slug == "nibbin-hollow"
    assert "nibbin-hollow" in listed.output and "youtube_shorts" in listed.output
    assert "missing: tiktok, instagram_reels, facebook_reels" in listed.output


def test_mapping_a_second_account_for_the_same_ip_and_platform_is_refused(cli_db: Engine) -> None:
    args = ["accounts", "map", "nibbin-hollow", "tiktok", "--channel-id", "a",
            "--external-account-id", "tt1"]  # fmt: skip
    runner.invoke(app, args)

    again = runner.invoke(app, [*args[:-1], "tt2"])

    assert again.exit_code != 0
    assert "already has" in again.output


def test_mapping_to_an_unknown_ip_is_refused(cli_db: Engine) -> None:
    result = runner.invoke(
        app, ["accounts", "map", "no-such-ip", "tiktok", "--channel-id", "a",
              "--external-account-id", "x"],
    )  # fmt: skip

    assert result.exit_code != 0
    assert "no IP" in result.output


def test_listing_buffer_channels_without_a_key_explains_what_is_missing(cli_db: Engine) -> None:
    result = runner.invoke(app, ["accounts", "channels"])

    assert result.exit_code != 0
    assert "HATCH_BUFFER_API_KEY" in result.output
