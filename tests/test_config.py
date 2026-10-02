from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ENV_FILES, Settings, get_settings


def test_default_budget_limits_match_the_prd_contract() -> None:
    settings = Settings(_env_file=None)

    assert settings.budget_target_per_video_usd == Decimal("0.50")
    assert settings.budget_max_per_video_usd == Decimal("1.50")
    assert settings.budget_daily_usd == Decimal("15.00")
    assert settings.budget_monthly_usd == Decimal("500.00")


def test_settings_are_overridden_by_hatch_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HATCH_ENV", "test")
    monkeypatch.setenv("HATCH_BUDGET_DAILY_USD", "20")

    settings = Settings(_env_file=None)

    assert settings.env == "test"
    assert settings.budget_daily_usd == Decimal("20")


def test_higgsfield_credentials_fall_back_to_the_sdk_style_hf_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_KEY", "key-id-1:key-secret-1")

    assert Settings(_env_file=None).higgsfield_credentials() == ("key-id-1", "key-secret-1")


def test_explicit_hatch_higgsfield_credentials_win_over_hf_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_KEY", "key-id-1:key-secret-1")
    monkeypatch.setenv("HATCH_HIGGSFIELD_API_KEY", "key-id-2")
    monkeypatch.setenv("HATCH_HIGGSFIELD_API_SECRET", "key-secret-2")

    assert Settings(_env_file=None).higgsfield_credentials() == ("key-id-2", "key-secret-2")


def test_no_higgsfield_credentials_means_none() -> None:
    assert Settings(_env_file=None).higgsfield_credentials() is None


def test_a_malformed_hf_key_fails_without_revealing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_KEY", "only-one-part-s3cr3t")

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    assert "<key id>:<key secret>" in str(error.value)
    assert "s3cr3t" not in str(error.value)


def test_env_local_is_read_after_env_and_overrides_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("HATCH_ENV=from-env\nHATCH_LOG_LEVEL=DEBUG\n")
    (tmp_path / ".env.local").write_text("HATCH_ENV=from-env-local\nHF_KEY=key-id-3:secret-3\n")

    settings = Settings(_env_file=ENV_FILES)

    assert ENV_FILES == (".env", ".env.local")
    assert settings.env == "from-env-local"
    assert settings.log_level == "DEBUG"
    assert settings.higgsfield_credentials() == ("key-id-3", "secret-3")


def test_tests_never_read_the_developers_env_files() -> None:
    assert Settings.model_config["env_file"] is None
    assert get_settings().higgsfield_credentials() is None
