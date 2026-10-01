import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.evidence import experiment_evidence
from app.evolution.models import DecisionType, ParentRelation, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import (
    evaluate_replication,
    find_potential_winners,
    request_replication,
)
from app.experiments.models import Experiment
from app.experiments.states import ExperimentConclusion, ExperimentStatus, VideoStatus
from app.platforms import Platform
from tests.evolution.helpers import evaluated

POLICY = EvolutionPolicy()


def decisions(session: Session, kind: DecisionType) -> list[SelectionDecision]:
    return list(
        session.scalars(select(SelectionDecision).where(SelectionDecision.decision_type == kind))
    )


# --- experiment evidence -----------------------------------------------------------


def test_experiment_evidence_combines_platforms_without_pooling_raw_signals(
    session: Session,
) -> None:
    experiment = evaluated(session, 0.8, platforms=(Platform.YOUTUBE_SHORTS, Platform.TIKTOK))

    evidence = experiment_evidence(session, experiment)

    assert evidence.sufficient
    assert evidence.score == pytest.approx(0.8)
    assert set(evidence.platforms) == {"youtube_shorts", "tiktok"}


def test_an_experiment_without_fitness_has_no_evidence(session: Session) -> None:
    evidence = experiment_evidence(session, evaluated(session, None))

    assert evidence.score is None and not evidence.sufficient


def test_low_confidence_fitness_is_not_sufficient_evidence(session: Session) -> None:
    evidence = experiment_evidence(session, evaluated(session, 0.95, confidence=0.05))

    assert not evidence.sufficient


# --- potential winners and replication requests ----------------------------------------


def test_a_high_scoring_experiment_is_a_potential_winner_not_a_winner(session: Session) -> None:
    viral = evaluated(session, 0.92)
    evaluated(session, 0.50)

    winners = find_potential_winners(session, viral.ip, POLICY)

    assert [w.id for w in winners] == [viral.id]
    assert viral.conclusion is None  # one observation proves nothing yet


def test_requesting_replication_is_recorded_with_its_evidence_once(session: Session) -> None:
    viral = evaluated(session, 0.92)

    first = request_replication(session, viral, POLICY)
    second = request_replication(session, viral, POLICY)

    assert first is not None and second is None
    [decision] = decisions(session, DecisionType.REQUEST_REPLICATION)
    assert decision.subject_experiment_id == viral.id
    assert decision.evidence["score"] == pytest.approx(0.92)
    assert decision.evidence["descendants_requested"] == 3
    assert "0.92" in decision.reason
    assert find_potential_winners(session, viral.ip, POLICY) == []  # already being replicated


def test_replication_descendants_are_never_themselves_potential_winners(session: Session) -> None:
    viral = evaluated(session, 0.92)
    request_replication(session, viral, POLICY)
    evaluated(session, 0.95, parent=viral)

    assert find_potential_winners(session, viral.ip, POLICY) == []


# --- verdicts ------------------------------------------------------------------------------


def replicate(session: Session, original: float, descendants: list[float | None]) -> Experiment:
    winner = evaluated(session, original)
    request_replication(session, winner, POLICY)
    for score in descendants:
        evaluated(session, score, parent=winner)
    return winner


def test_verdict_waits_until_enough_descendants_have_evidence(session: Session) -> None:
    winner = replicate(session, 0.9, [0.7, 0.7, None])

    assert evaluate_replication(session, winner, POLICY) is None
    assert winner.conclusion is None


@pytest.mark.parametrize(
    ("original", "descendants", "expected"),
    [
        (0.90, [0.70, 0.66, 0.60], ExperimentConclusion.SUPPORTED),
        (0.90, [0.70, 0.62, 0.40], ExperimentConclusion.SUPPORTED),
        (0.90, [0.70, 0.45, 0.40], ExperimentConclusion.PARTIALLY_SUPPORTED),
        (0.70, [0.50, 0.45, 0.40], ExperimentConclusion.NOT_SUPPORTED),
        (0.95, [0.45, 0.42, 0.40], ExperimentConclusion.ANOMALOUS),
    ],
)
def test_replication_verdicts(
    session: Session,
    original: float,
    descendants: list[float | None],
    expected: ExperimentConclusion,
) -> None:
    winner = replicate(session, original, descendants)

    conclusion = evaluate_replication(session, winner, POLICY)

    assert conclusion is expected
    assert winner.conclusion is expected
    assert winner.status is ExperimentStatus.CONCLUDED
    [decision] = decisions(session, DecisionType.CONCLUDE_EXPERIMENT)
    assert decision.subject_experiment_id == winner.id
    assert decision.evidence["conclusion"] == expected.value
    assert len(decision.evidence["descendants"]) == 3
    assert decision.evidence["original_score"] == pytest.approx(original)


def test_descendants_that_never_produced_evidence_are_replaced_up_to_the_limit(
    session: Session,
) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    evaluated(session, 0.7, parent=winner)
    evaluated(session, 0.7, parent=winner)
    evaluated(session, None, parent=winner, video_status=VideoStatus.QA_REJECTED)

    assert evaluate_replication(session, winner, POLICY) is None
    [request] = decisions(session, DecisionType.REQUEST_REPLICATION)[1:]
    assert request.evidence["descendants_requested"] == 1
    assert "replace" in request.reason.lower()


def test_when_the_descendant_limit_is_spent_without_evidence_it_is_inconclusive(
    session: Session,
) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    for _ in range(POLICY.replication_max_descendants):
        evaluated(session, None, parent=winner, video_status=VideoStatus.GENERATION_FAILED)

    assert evaluate_replication(session, winner, POLICY) is ExperimentConclusion.INCONCLUSIVE


def test_an_experiment_is_concluded_only_once(session: Session) -> None:
    winner = replicate(session, 0.9, [0.7, 0.7, 0.7])
    evaluate_replication(session, winner, POLICY)

    assert evaluate_replication(session, winner, POLICY) is ExperimentConclusion.SUPPORTED
    assert len(decisions(session, DecisionType.CONCLUDE_EXPERIMENT)) == 1


def test_only_replication_descendants_count_as_replication_evidence(session: Session) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    for _ in range(3):
        evaluated(session, 0.8, parent=winner, relation=ParentRelation.MUTATION)

    assert evaluate_replication(session, winner, POLICY) is None
    assert session.get_one(Experiment, winner.id).conclusion is None
