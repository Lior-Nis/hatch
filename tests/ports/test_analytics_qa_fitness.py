from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.analytics.ports import AnalyticsAdapter, NormalizedMetrics
from app.fitness.ports import FitnessEvaluator, FitnessInputs
from app.platforms import Platform
from app.quality.ports import QACandidate, QAGate, QAOutcome
from integrations.fake.analytics import FakeAnalyticsAdapter
from integrations.fake.fitness import FakeFitnessEvaluator
from integrations.fake.quality import FakeQAGate


def test_analytics_adapter_returns_raw_payload_and_normalized_metrics_separately() -> None:
    adapter: AnalyticsAdapter = FakeAnalyticsAdapter(
        platform=Platform.TIKTOK,
        payloads={"post-9": {"play_count": 1200, "full_video_watched_rate": 0.41}},
    )

    observation = adapter.fetch_post_metrics(
        platform_account_id="tt-account-1", platform_post_id="post-9"
    )

    assert observation.platform is Platform.TIKTOK
    assert observation.raw == {"play_count": 1200, "full_video_watched_rate": 0.41}
    assert observation.normalized.views == 1200
    assert observation.normalized.completion_rate == 0.41
    assert observation.observed_at.tzinfo is not None


def test_qa_gate_returns_a_structured_verdict_with_reasons() -> None:
    gate: QAGate = FakeQAGate(
        name="child_safety", mandatory=True, outcome=QAOutcome.FAIL, reasons=["scary monster"]
    )
    candidate = QACandidate(
        experiment_id="exp-1", media_path=Path("short.mp4"), genes={}, creative_spec=""
    )

    verdict = gate.evaluate(candidate)

    assert gate.mandatory is True
    assert verdict.outcome is QAOutcome.FAIL
    assert verdict.reasons == ("scary monster",)
    assert verdict.gate == "child_safety"
    assert verdict.gate_version == gate.version


def test_fitness_evaluator_reports_score_with_formula_version_and_inputs() -> None:
    evaluator: FitnessEvaluator = FakeFitnessEvaluator(score=0.7)
    inputs = FitnessInputs(
        platform=Platform.YOUTUBE_SHORTS,
        metrics=NormalizedMetrics(views=500, completion_rate=0.5),
        hours_since_publication=72.0,
        production_cost_usd=Decimal("0.42"),
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    result = evaluator.evaluate(inputs)

    assert result.score == 0.7
    assert result.evaluator == evaluator.name
    assert result.evaluator_version == evaluator.version
    assert result.inputs == inputs
