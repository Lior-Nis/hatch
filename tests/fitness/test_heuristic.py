from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.analytics.ports import NormalizedMetrics
from app.fitness.heuristic import HeuristicFitnessEvaluator, InsufficientSignals
from app.fitness.ports import FitnessEvaluator, FitnessInputs
from app.platforms import Platform

BASELINE = NormalizedMetrics(
    views=1000,
    average_watch_fraction=0.60,
    completion_rate=0.30,
    likes=30,
    shares=3,
    saves=5,
    comments=2,
    follows=2,
)


def inputs(platform: Platform = Platform.TIKTOK, **overrides: Any) -> FitnessInputs:
    fields: dict[str, Any] = {
        "platform": platform,
        "metrics": BASELINE,
        "hours_since_publication": 72.0,
        "production_cost_usd": Decimal("0.40"),
        "target_cost_usd": Decimal("0.50"),
        "duration_seconds": 8.0,
        "observed_at": datetime(2026, 10, 4, tzinfo=UTC),
        "baseline": BASELINE,
    }
    fields.update(overrides)
    return FitnessInputs.model_validate(fields)


def metrics(**changes: Any) -> NormalizedMetrics:
    return BASELINE.model_copy(update=changes)


def score(platform: Platform = Platform.TIKTOK, **overrides: Any) -> float:
    return HeuristicFitnessEvaluator().evaluate(inputs(platform, **overrides)).score


def test_evaluator_satisfies_the_contract_and_is_versioned() -> None:
    evaluator: FitnessEvaluator = HeuristicFitnessEvaluator()

    assert (evaluator.name, evaluator.version) == ("heuristic", "1")


def test_a_video_that_matches_its_account_baseline_scores_in_the_middle() -> None:
    assert score(production_cost_usd=Decimal("0.50")) == pytest.approx(0.5)


def test_better_retention_and_sharing_raise_fitness_and_worse_lower_it() -> None:
    strong = score(metrics=metrics(average_watch_fraction=0.85, completion_rate=0.5, shares=9))
    weak = score(metrics=metrics(average_watch_fraction=0.35, completion_rate=0.1, shares=1))

    assert strong > 0.6
    assert weak < 0.4


def test_score_is_always_between_zero_and_one() -> None:
    extreme = metrics(
        views=10_000_000, average_watch_fraction=5.0, completion_rate=1.0, likes=9_000_000,
        shares=9_000_000, saves=9_000_000, follows=9_000_000,
    )  # fmt: skip
    dead = metrics(average_watch_fraction=0.0, completion_rate=0.0, likes=0, shares=0, saves=0,
                   comments=0, follows=0)  # fmt: skip

    assert 0.0 <= score(metrics=dead) < 0.2
    assert 0.8 < score(metrics=extreme) <= 1.0


# --- never raw views alone -------------------------------------------------------


def test_views_alone_cannot_produce_a_fitness_score() -> None:
    only_views = NormalizedMetrics(views=5_000_000)

    with pytest.raises(InsufficientSignals, match="views alone"):
        HeuristicFitnessEvaluator().evaluate(inputs(metrics=only_views))


def test_without_a_views_baseline_more_views_do_not_change_the_score() -> None:
    priors_only: dict[str, Any] = {"baseline": None}
    small = score(metrics=metrics(views=1000, likes=30, shares=3, saves=5, comments=2, follows=2),
                  **priors_only)  # fmt: skip
    huge = score(
        metrics=metrics(
            views=100_000, likes=3000, shares=300, saves=500, comments=200, follows=200
        ),
        **priors_only,
    )

    assert small == pytest.approx(huge)


def test_a_viral_but_poorly_watched_video_scores_below_a_well_watched_normal_one() -> None:
    viral = metrics(views=100_000, average_watch_fraction=0.25, completion_rate=0.05,
                    likes=1000, shares=50, saves=100, comments=50, follows=20)  # fmt: skip
    loved = metrics(views=1000, average_watch_fraction=0.85, completion_rate=0.55,
                    likes=60, shares=8, saves=10, comments=4, follows=5)  # fmt: skip

    assert score(metrics=viral) < score(metrics=loved)


def test_too_few_views_is_insufficient_evidence() -> None:
    with pytest.raises(InsufficientSignals, match="views"):
        HeuristicFitnessEvaluator().evaluate(inputs(metrics=metrics(views=12)))


# --- missing is not zero ----------------------------------------------------------


def test_signals_a_platform_does_not_report_do_not_lower_the_score() -> None:
    full = score(Platform.INSTAGRAM_REELS)
    without_saves = score(
        Platform.INSTAGRAM_REELS,
        metrics=metrics(saves=None),
        baseline=BASELINE.model_copy(update={"saves": None}),
    )

    assert without_saves == pytest.approx(full)


def test_youtube_ignores_comments_and_saves_which_are_disabled_for_kids_content() -> None:
    silenced = metrics(comments=0, saves=0)

    assert score(Platform.YOUTUBE_SHORTS, metrics=silenced) == pytest.approx(
        score(Platform.YOUTUBE_SHORTS)
    )
    assert score(Platform.TIKTOK, metrics=silenced) < score(Platform.TIKTOK)


def test_average_watch_fraction_is_derived_from_watch_seconds_when_needed() -> None:
    derived = metrics(average_watch_fraction=None, average_watch_seconds=6.8)  # 85% of 8s

    result = HeuristicFitnessEvaluator().evaluate(inputs(metrics=derived))

    assert result.components["retention"] > 0


# --- what is recorded --------------------------------------------------------------


def test_result_records_formula_version_inputs_components_and_references() -> None:
    given = inputs(metrics=metrics(average_watch_fraction=0.8))

    result = HeuristicFitnessEvaluator().evaluate(given)

    assert (result.evaluator, result.evaluator_version) == ("heuristic", "1")
    assert result.inputs == given
    assert result.components["retention"] > 0
    assert set(result.components) >= {"retention", "completion", "like_rate", "share_rate"}
    retention = result.details["references"]["retention"]
    assert retention == {"value": 0.6, "source": "account_baseline"}
    assert result.details["weights"]["retention"] > 0
    assert 0 < result.confidence <= 1


def test_without_account_history_platform_priors_are_used_and_labelled() -> None:
    result = HeuristicFitnessEvaluator().evaluate(inputs(baseline=None))

    references = result.details["references"]
    sources = {ref["source"] for name, ref in references.items() if name != "cost"}
    assert sources == {"platform_prior"}
    assert "distribution" not in result.components  # no baseline, so views cannot be judged


def test_confidence_grows_with_views() -> None:
    evaluator = HeuristicFitnessEvaluator()
    few = evaluator.evaluate(inputs(metrics=metrics(views=60, likes=2, shares=0, saves=0)))
    many = evaluator.evaluate(inputs(metrics=metrics(views=5000, likes=150, shares=15, saves=25)))

    assert few.confidence < many.confidence == 1.0


def test_overspending_on_production_lowers_fitness_slightly() -> None:
    cheap = score(production_cost_usd=Decimal("0.40"))
    expensive = score(production_cost_usd=Decimal("1.40"))

    assert 0 < cheap - expensive < 0.1


def test_platforms_weigh_signals_differently() -> None:
    evaluator = HeuristicFitnessEvaluator()
    tiktok = evaluator.evaluate(inputs(Platform.TIKTOK)).details["weights"]
    youtube = evaluator.evaluate(inputs(Platform.YOUTUBE_SHORTS)).details["weights"]

    assert tiktok != youtube
    assert "comment_rate" not in youtube and "save_rate" not in youtube
