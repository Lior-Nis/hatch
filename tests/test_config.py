from decimal import Decimal

import pytest

from app.config import Settings


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
