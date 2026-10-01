from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.fitness.ip_aggregate import (
    IP_AGGREGATE_VERSION,
    IPAggregationPolicy,
    aggregate_ip_fitness,
    weighted_median,
)
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.platforms import Platform
from tests.factories import make_experiment

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def video_fitness(
    session: Session,
    experiment: Experiment,
    score: float,
    *,
    platform: Platform = Platform.YOUTUBE_SHORTS,
    checkpoint: str = "72h",
    confidence: float = 1.0,
    at: datetime = T0,
) -> None:
    session.add(
        FitnessSnapshot(
            scope=FitnessScope.VIDEO,
            ip_id=experiment.ip_id,
            experiment=experiment,
            platform=platform,
            checkpoint=checkpoint,
            evaluator="heuristic",
            evaluator_version="1",
            score=score,
            components={},
            inputs={"confidence": confidence},
            created_at=at,
        )
    )
    session.flush()


def ip_with_scores(
    session: Session, scores: list[float], platform: Platform = Platform.YOUTUBE_SHORTS
) -> Experiment:
    first = None
    for index, score in enumerate(scores):
        experiment = make_experiment(session)
        first = first or experiment
        video_fitness(session, experiment, score, platform=platform, at=T0 + timedelta(hours=index))
    assert first is not None
    return first


def test_weighted_median_ignores_outliers_and_respects_weights() -> None:
    assert weighted_median([(0.8, 1), (0.82, 1), (0.05, 1)]) == pytest.approx(0.8)
    assert weighted_median([(0.2, 0.1), (0.9, 1.0)]) == pytest.approx(0.9)
    assert weighted_median([(0.4, 1)]) == 0.4


def test_one_poor_video_does_not_sink_a_strong_ip(session: Session) -> None:
    strong = ip_with_scores(session, [0.78, 0.81, 0.75, 0.80, 0.79])
    before = aggregate_ip_fitness(session, strong.ip)
    flop = make_experiment(session)
    video_fitness(session, flop, 0.05, at=T0 + timedelta(days=1))

    after = aggregate_ip_fitness(session, strong.ip)

    assert before.score > 0.75
    assert after.score > 0.75
    assert before.score - after.score < 0.03
    assert after.inputs["sufficient_evidence"] is True


def test_platforms_are_aggregated_separately_before_being_combined(session: Session) -> None:
    experiments = [make_experiment(session) for _ in range(4)]
    for experiment in experiments:
        video_fitness(session, experiment, 0.8, platform=Platform.YOUTUBE_SHORTS)
        video_fitness(session, experiment, 0.3, platform=Platform.TIKTOK)

    snapshot = aggregate_ip_fitness(session, experiments[0].ip)

    assert snapshot.components == {"youtube_shorts": 0.8, "tiktok": 0.3}
    weights = snapshot.inputs["platform_weights"]
    expected = (0.8 * weights["youtube_shorts"] + 0.3 * weights["tiktok"]) / (
        weights["youtube_shorts"] + weights["tiktok"]
    )
    assert snapshot.score == pytest.approx(expected)
    assert weights["youtube_shorts"] > weights["tiktok"]  # the primary children's surface


def test_a_platform_with_too_few_videos_is_reported_but_not_counted(session: Session) -> None:
    experiments = [make_experiment(session) for _ in range(3)]
    for experiment in experiments:
        video_fitness(session, experiment, 0.7, platform=Platform.YOUTUBE_SHORTS)
    video_fitness(session, experiments[0], 0.1, platform=Platform.FACEBOOK_REELS)

    snapshot = aggregate_ip_fitness(session, experiments[0].ip)

    assert snapshot.score == pytest.approx(0.7)
    facebook = snapshot.inputs["platforms"]["facebook_reels"]
    assert (facebook["videos"], facebook["sufficient"]) == (1, False)
    assert "facebook_reels" not in snapshot.components


def test_without_enough_videos_anywhere_the_evidence_is_flagged_insufficient(
    session: Session,
) -> None:
    lonely = ip_with_scores(session, [0.95])

    snapshot = aggregate_ip_fitness(session, lonely.ip)

    assert snapshot.inputs["sufficient_evidence"] is False
    assert snapshot.score == pytest.approx(0.5)  # neutral: one video proves nothing


def test_the_most_mature_evaluation_of_each_video_is_used(session: Session) -> None:
    experiments = [make_experiment(session) for _ in range(3)]
    for experiment in experiments:
        video_fitness(session, experiment, 0.9, checkpoint="72h", at=T0)
        video_fitness(session, experiment, 0.4, checkpoint="30d", at=T0 + timedelta(days=27))

    snapshot = aggregate_ip_fitness(session, experiments[0].ip)

    assert snapshot.score == pytest.approx(0.4)


def test_only_the_most_recent_videos_count(session: Session) -> None:
    old_then_new = ip_with_scores(session, [0.1, 0.1, 0.1, 0.9, 0.9, 0.9])

    snapshot = aggregate_ip_fitness(
        session, old_then_new.ip, policy=IPAggregationPolicy(window=3, min_videos=3)
    )

    assert snapshot.score == pytest.approx(0.9)


def test_low_confidence_scores_carry_less_weight(session: Session) -> None:
    experiments = [make_experiment(session) for _ in range(3)]
    video_fitness(session, experiments[0], 0.2, confidence=0.05)
    video_fitness(session, experiments[1], 0.8, confidence=1.0)
    video_fitness(session, experiments[2], 0.8, confidence=1.0)

    assert aggregate_ip_fitness(session, experiments[0].ip).score == pytest.approx(0.8)


def test_ip_fitness_is_a_versioned_inspectable_snapshot(session: Session) -> None:
    experiment = ip_with_scores(session, [0.6, 0.7, 0.8])

    snapshot = aggregate_ip_fitness(session, experiment.ip)

    stored = session.scalars(
        select(FitnessSnapshot).where(FitnessSnapshot.scope == FitnessScope.IP)
    ).one()
    assert stored.id == snapshot.id
    assert stored.experiment_id is None and stored.ip_id == experiment.ip_id
    assert (stored.evaluator, stored.evaluator_version) == ("ip_aggregate", IP_AGGREGATE_VERSION)
    youtube = stored.inputs["platforms"]["youtube_shorts"]
    assert youtube["videos"] == 3
    assert len(youtube["experiments"]) == 3
    assert {"experiment_id", "score", "confidence", "checkpoint"} <= set(youtube["experiments"][0])
    assert stored.inputs["policy"]["min_videos"] == 3


def test_ips_do_not_see_each_others_videos(session: Session) -> None:
    ours = ip_with_scores(session, [0.8, 0.8, 0.8])
    for _ in range(3):
        other = make_experiment(session, ip_slug="zed-lab")
        video_fitness(session, other, 0.1)

    assert aggregate_ip_fitness(session, ours.ip).score == pytest.approx(0.8)
