from collections import Counter

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.allocation import choose_bucket
from app.evolution.models import AllocationBucket, DecisionType, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import evaluate_replication, request_replication
from app.evolution.selection import select_parent
from app.experiments.models import Experiment
from app.ips.models import IP
from tests.evolution.helpers import evaluated

POLICY = EvolutionPolicy()
ALL = {AllocationBucket.EXPLOIT, AllocationBucket.MUTATE, AllocationBucket.EXPLORE}


def record(
    session: Session, ip: IP, bucket: AllocationBucket, subject: Experiment | None = None
) -> None:
    session.add(
        SelectionDecision(
            decision_type=DecisionType.CREATE_EXPERIMENT,
            bucket=bucket,
            ip=ip,
            subject_experiment=subject,
            reason="test",
            evidence={},
            policy_version=POLICY.version,
        )
    )
    session.flush()


def allocate(
    session: Session, ip: IP, slots: int, available: set[AllocationBucket]
) -> list[AllocationBucket]:
    chosen = []
    for _ in range(slots):
        bucket, _ = choose_bucket(session, ip, POLICY, available=available)
        record(session, ip, bucket)
        chosen.append(bucket)
    return chosen


def supported_lineage(session: Session, score: float = 0.9, descendants: float = 0.7) -> Experiment:
    winner = evaluated(session, score)
    request_replication(session, winner, POLICY)
    for _ in range(3):
        evaluated(session, descendants, parent=winner)
    evaluate_replication(session, winner, POLICY)
    return winner


# --- allocation ----------------------------------------------------------------------


def test_with_no_evidence_everything_is_exploration(session: Session) -> None:
    ip = evaluated(session, None).ip

    chosen = allocate(session, ip, 5, {AllocationBucket.EXPLORE})

    assert chosen == [AllocationBucket.EXPLORE] * 5


def test_allocation_converges_on_sixty_twenty_five_fifteen(session: Session) -> None:
    ip = evaluated(session, None).ip

    shares = Counter(allocate(session, ip, 100, ALL))

    assert shares[AllocationBucket.EXPLOIT] == pytest.approx(60, abs=3)
    assert shares[AllocationBucket.MUTATE] == pytest.approx(25, abs=3)
    assert shares[AllocationBucket.EXPLORE] == pytest.approx(15, abs=3)


def test_exploration_never_falls_to_zero(session: Session) -> None:
    ip = evaluated(session, None).ip

    chosen = allocate(session, ip, 60, ALL)

    for start in range(len(chosen) - 10):
        assert AllocationBucket.EXPLORE in chosen[start : start + 10]


def test_an_unavailable_bucket_gives_its_share_away_without_starving_exploration(
    session: Session,
) -> None:
    ip = evaluated(session, None).ip

    shares = Counter(allocate(session, ip, 40, {AllocationBucket.MUTATE, AllocationBucket.EXPLORE}))

    assert shares[AllocationBucket.EXPLOIT] == 0
    assert shares[AllocationBucket.EXPLORE] >= 0.15 * 40
    assert shares[AllocationBucket.MUTATE] > shares[AllocationBucket.EXPLORE]


def test_the_choice_comes_with_the_shares_it_was_based_on(session: Session) -> None:
    ip = evaluated(session, None).ip
    record(session, ip, AllocationBucket.EXPLORE)
    record(session, ip, AllocationBucket.EXPLORE)

    bucket, evidence = choose_bucket(session, ip, POLICY, available=ALL)

    assert bucket is AllocationBucket.EXPLOIT
    assert evidence["recent_counts"] == {"exploit": 0, "mutate": 0, "explore": 2}
    assert evidence["targets"] == {"exploit": 0.6, "mutate": 0.25, "explore": 0.15}
    assert evidence["available"] == ["exploit", "explore", "mutate"]


def test_only_the_recent_window_counts(session: Session) -> None:
    ip = evaluated(session, None).ip
    for _ in range(POLICY.allocation_window):
        record(session, ip, AllocationBucket.EXPLORE)
    for _ in range(POLICY.allocation_window):
        record(session, ip, AllocationBucket.EXPLOIT)

    bucket, evidence = choose_bucket(session, ip, POLICY, available=ALL)

    assert evidence["recent_counts"]["explore"] == 0
    assert bucket is not AllocationBucket.EXPLOIT


# --- parent selection ------------------------------------------------------------------


def test_exploit_selects_a_proven_lineage(session: Session) -> None:
    proven = supported_lineage(session)
    evaluated(session, 0.95)  # brilliant but unreplicated: not exploitable

    choice = select_parent(session, proven.ip, POLICY, bucket=AllocationBucket.EXPLOIT)

    assert choice is not None and choice.parent.id == proven.id


def test_exploit_has_nothing_to_select_without_a_proven_lineage(session: Session) -> None:
    unproven = evaluated(session, 0.95)

    assert select_parent(session, unproven.ip, POLICY, bucket=AllocationBucket.EXPLOIT) is None


def test_mutation_selects_the_best_promising_experiment(session: Session) -> None:
    good = evaluated(session, 0.62)
    evaluated(session, 0.55)
    evaluated(session, 0.30)  # below the promising threshold

    choice = select_parent(session, good.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is not None and choice.parent.id == good.id
    scores = {c["experiment_id"]: c["score"] for c in choice.evidence["candidates"]}
    assert len(scores) == 2  # the weak one is not a candidate at all


def test_refuted_and_anomalous_experiments_are_never_parents(session: Session) -> None:
    refuted = evaluated(session, 0.9)
    request_replication(session, refuted, POLICY)
    for _ in range(3):
        evaluated(session, 0.3, parent=refuted)
    evaluate_replication(session, refuted, POLICY)

    choice = select_parent(session, refuted.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is None or choice.parent.id != refuted.id


def test_a_lineage_that_dominates_recent_production_yields_to_another(session: Session) -> None:
    strong = evaluated(session, 0.64)
    other = evaluated(session, 0.58)
    for _ in range(6):
        record(session, strong.ip, AllocationBucket.MUTATE, subject=strong)
    record(session, strong.ip, AllocationBucket.MUTATE, subject=other)

    choice = select_parent(session, strong.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is not None and choice.parent.id == other.id
    strong_entry = next(
        c for c in choice.evidence["candidates"] if c["experiment_id"] == str(strong.id)
    )
    assert strong_entry["lineage_share"] > POLICY.max_lineage_share
    assert "share" in strong_entry["excluded_because"]


def test_a_dominant_lineage_is_still_used_when_it_is_the_only_one(session: Session) -> None:
    only = evaluated(session, 0.64)
    for _ in range(6):
        record(session, only.ip, AllocationBucket.MUTATE, subject=only)

    choice = select_parent(session, only.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is not None and choice.parent.id == only.id


def test_selection_is_reproducible_from_its_stored_inputs(session: Session) -> None:
    first = evaluated(session, 0.60)
    evaluated(session, 0.60)
    evaluated(session, 0.57)

    choice = select_parent(session, first.ip, POLICY, bucket=AllocationBucket.MUTATE)
    again = select_parent(session, first.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is not None and again is not None
    assert choice.parent.id == again.parent.id == first.id  # ties go to the older experiment
    eligible = [c for c in choice.evidence["candidates"] if c["excluded_because"] is None]
    replayed = max(eligible, key=lambda c: (c["adjusted_score"], -c["rank_order"]))
    assert replayed["experiment_id"] == str(choice.parent.id)
    assert choice.evidence["rule"]


def test_parents_come_only_from_the_same_ip(session: Session) -> None:
    ours = evaluated(session, 0.55)
    evaluated(session, 0.95, ip_slug="zed-lab")

    choice = select_parent(session, ours.ip, POLICY, bucket=AllocationBucket.MUTATE)

    assert choice is not None and choice.parent.id == ours.id
    assert len(session.scalars(select(IP)).all()) == 2
