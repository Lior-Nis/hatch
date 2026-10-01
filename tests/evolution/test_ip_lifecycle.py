from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.evolution.ip_lifecycle import resurrect_ip, review_ip_lifecycle
from app.evolution.models import DecisionType, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import evaluate_replication, request_replication
from app.experiments.models import Experiment
from app.fitness.models import FitnessSnapshot
from app.ips.models import IP
from app.ips.states import IPStatus
from tests.evolution.helpers import evaluated

POLICY = EvolutionPolicy()


def settle(session: Session, ip: IP, rounds: int = 6) -> IPStatus:
    """Review the IP until its status stops changing."""
    for _ in range(rounds):
        if review_ip_lifecycle(session, ip, POLICY) is None:
            break
    return ip.status


def decisions(session: Session, kind: DecisionType) -> list[SelectionDecision]:
    return list(
        session.scalars(
            select(SelectionDecision)
            .where(SelectionDecision.decision_type == kind)
            .order_by(SelectionDecision.created_at)
        )
    )


def supported_lineage(session: Session, ip_slug: str | None = None) -> Experiment:
    winner = evaluated(session, 0.9, ip_slug=ip_slug)
    request_replication(session, winner, POLICY)
    for _ in range(3):
        evaluated(session, 0.7, parent=winner, ip_slug=ip_slug)
    evaluate_replication(session, winner, POLICY)
    return winner


# --- promotion needs replication ---------------------------------------------------


def test_an_ip_with_experiments_becomes_experimental(session: Session) -> None:
    ip = evaluated(session, None).ip

    decision = review_ip_lifecycle(session, ip, POLICY)

    assert ip.status is IPStatus.EXPERIMENTAL
    assert decision is not None and decision.decision_type is DecisionType.PROMOTE_IP


def test_one_viral_video_cannot_validate_or_scale_an_ip(session: Session) -> None:
    viral = evaluated(session, 0.97)
    for _ in range(4):
        evaluated(session, 0.50)

    status = settle(session, viral.ip)

    assert status is IPStatus.PROMISING  # noticed, but proven nothing
    reasons = " ".join(d.reason for d in decisions(session, DecisionType.PROMOTE_IP))
    assert "validated" not in reasons.lower() and "scaled" not in reasons.lower()


def test_replicated_evidence_validates_the_ip(session: Session) -> None:
    winner = supported_lineage(session)

    status = settle(session, winner.ip)

    assert status is IPStatus.VALIDATED
    promotion = decisions(session, DecisionType.PROMOTE_IP)[-1]
    assert promotion.evidence["supported_lineages"] == 1
    assert promotion.evidence["to"] == "validated"
    assert str(winner.lineage_id) in promotion.evidence["supported_lineage_ids"]


def test_scaling_needs_several_independently_replicated_lineages(session: Session) -> None:
    first = supported_lineage(session)
    assert settle(session, first.ip) is IPStatus.VALIDATED

    supported_lineage(session)

    assert settle(session, first.ip) is IPStatus.SCALED


def test_every_promotion_is_recorded_with_scores_and_thresholds(session: Session) -> None:
    winner = supported_lineage(session)
    settle(session, winner.ip)

    promotions = decisions(session, DecisionType.PROMOTE_IP)

    assert [d.evidence["to"] for d in promotions] == ["experimental", "promising", "validated"]
    assert all(d.ip_id == winner.ip_id and d.policy_version == POLICY.version for d in promotions)
    assert all("ip_score" in d.evidence for d in promotions)


# --- archive -------------------------------------------------------------------------


def weak_ip(session: Session, videos: int, score: float = 0.25) -> IP:
    for _ in range(videos):
        experiment = evaluated(session, score)
    return experiment.ip


def test_a_weak_ip_is_archived_only_after_sufficient_evidence(session: Session) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos - 1)
    assert settle(session, ip) is IPStatus.EXPERIMENTAL

    evaluated(session, 0.25)

    assert settle(session, ip) is IPStatus.ARCHIVED
    [archive] = decisions(session, DecisionType.ARCHIVE_IP)
    assert archive.evidence["videos"] == POLICY.ip_archive_min_videos
    assert archive.evidence["ip_score"] < POLICY.ip_archive_score
    assert "0.25" in archive.reason


def test_archiving_deletes_nothing(session: Session) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos)
    experiments_before = session.scalar(select(func.count()).select_from(Experiment))
    fitness_before = session.scalar(select(func.count()).select_from(FitnessSnapshot))

    settle(session, ip)

    assert ip.status is IPStatus.ARCHIVED
    assert session.scalar(select(func.count()).select_from(Experiment)) == experiments_before
    assert (session.scalar(select(func.count()).select_from(FitnessSnapshot)) or 0) >= (
        fitness_before or 0
    )


def test_one_poor_video_does_not_archive_a_healthy_ip(session: Session) -> None:
    for _ in range(POLICY.ip_archive_min_videos):
        healthy = evaluated(session, 0.6)
    evaluated(session, 0.02)

    assert settle(session, healthy.ip) is not IPStatus.ARCHIVED


def test_an_ip_with_a_winner_under_replication_is_not_archived(session: Session) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos)
    hopeful = evaluated(session, 0.9)
    request_replication(session, hopeful, POLICY)

    assert settle(session, ip) is not IPStatus.ARCHIVED


# --- resurrection ----------------------------------------------------------------------


def test_an_archived_ip_returns_to_experimental_when_new_evidence_is_strong(
    session: Session,
) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos)
    assert settle(session, ip) is IPStatus.ARCHIVED

    for _ in range(POLICY.ip_archive_min_videos + 2):
        evaluated(session, 0.75)  # e.g. videos already published that matured well

    assert settle(session, ip) is IPStatus.EXPERIMENTAL or ip.status is IPStatus.PROMISING
    [resurrection] = decisions(session, DecisionType.RESURRECT_IP)
    assert resurrection.evidence["from"] == "archived"
    assert resurrection.evidence["ip_score"] >= POLICY.ip_resurrect_score


def test_an_archived_ip_stays_archived_without_new_evidence(session: Session) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos)
    settle(session, ip)

    assert review_ip_lifecycle(session, ip, POLICY) is None
    assert ip.status is IPStatus.ARCHIVED


def test_the_operator_can_resurrect_an_ip_with_a_recorded_reason(session: Session) -> None:
    ip = weak_ip(session, POLICY.ip_archive_min_videos)
    settle(session, ip)

    decision = resurrect_ip(
        session, ip, POLICY, reason="New character design to test.", decided_by="lior"
    )

    assert ip.status is IPStatus.EXPERIMENTAL
    assert decision.decision_type is DecisionType.RESURRECT_IP
    assert decision.evidence["decided_by"] == "lior"
    assert "New character design" in decision.reason
