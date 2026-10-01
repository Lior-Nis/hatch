"""Relationship and integrity tests for the full V1 domain model."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics.models import MetricSnapshot
from app.characters.models import Character, CharacterVersion, ExperimentCharacter
from app.db import ImmutableEvidenceError
from app.evolution.models import (
    AllocationBucket,
    DecisionType,
    ExperimentParent,
    Mutation,
    MutationOperation,
    ParentRelation,
    SelectionDecision,
)
from app.experiments.lineage import get_lineage
from app.experiments.models import Experiment
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.knowledge.models import KnowledgeSummary
from app.platforms import Platform
from app.production.models import Asset, AssetKind, ProviderModel
from app.publishing.models import PlatformAccount, Publication
from app.quality.models import HumanReview, ReviewDecision
from app.scheduling.models import JobRun, JobStatus
from tests.factories import make_experiment

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def make_asset(session: Session, experiment: Experiment) -> Asset:
    asset = Asset(
        experiment=experiment,
        kind=AssetKind.FINAL_VIDEO,
        storage_uri="local://x.mp4",
        sha256="0" * 64,
        size_bytes=1,
        mime_type="video/mp4",
        media_info={},
    )
    session.add(asset)
    session.flush()
    return asset


def make_account(session: Session, experiment: Experiment) -> PlatformAccount:
    account = PlatformAccount(
        ip=experiment.ip,
        platform=Platform.YOUTUBE_SHORTS,
        external_account_id="UC123",
        publisher_profile_id="buffer-1",
        handle="@nibbinhollow",
    )
    session.add(account)
    session.flush()
    return account


def make_publication(session: Session, experiment: Experiment) -> Publication:
    publication = Publication(
        experiment=experiment,
        asset=make_asset(session, experiment),
        platform=Platform.YOUTUBE_SHORTS,
        platform_account=make_account(session, experiment),
        platform_post_id="yt-1",
        published_at=NOW,
    )
    session.add(publication)
    session.flush()
    return publication


def make_metric(session: Session, publication: Publication) -> MetricSnapshot:
    snapshot = MetricSnapshot(
        publication=publication,
        observed_at=NOW,
        hours_since_publication=72.0,
        checkpoint="72h",
        adapter="fake",
        adapter_version="1",
        raw={"viewCount": "1200"},
        normalized={"views": 1200},
    )
    session.add(snapshot)
    session.flush()
    return snapshot


# --- characters ----------------------------------------------------------


def test_character_versions_belong_to_an_ip_and_are_used_by_experiments(session: Session) -> None:
    experiment = make_experiment(session)
    nib = Character(ip=experiment.ip, name="Nib", role="protagonist")
    v1 = CharacterVersion(character=nib, version=1, description="Leaf cape.", visual_spec={})
    v2 = CharacterVersion(character=nib, version=2, description="Acorn hat.", visual_spec={})
    session.add_all(
        [v1, v2, ExperimentCharacter(experiment=experiment, character_version=v2, role="primary")]
    )
    session.flush()
    session.expire_all()

    loaded = session.get_one(Experiment, experiment.id)
    assert [(c.character_version.version, c.role) for c in loaded.characters] == [(2, "primary")]
    character = loaded.characters[0].character_version.character
    assert character.ip_id == loaded.ip_id
    assert [v.version for v in character.versions] == [1, 2]


def test_a_character_cannot_have_two_versions_with_the_same_number(session: Session) -> None:
    nib = Character(ip=make_experiment(session).ip, name="Nib", role="protagonist")
    session.add_all(
        [
            CharacterVersion(character=nib, version=1, description="a", visual_spec={}),
            CharacterVersion(character=nib, version=1, description="b", visual_spec={}),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()


# --- parentage and mutations ---------------------------------------------


def test_descendant_records_its_parents_and_mutations_and_shares_the_lineage(
    session: Session,
) -> None:
    parent, donor = make_experiment(session), make_experiment(session)
    child = make_experiment(session, lineage_id=parent.lineage_id)
    session.add_all(
        [
            ExperimentParent(experiment=child, parent=parent, relation=ParentRelation.MUTATION),
            ExperimentParent(experiment=child, parent=donor, relation=ParentRelation.RECOMBINATION),
            Mutation(
                experiment=child,
                operation=MutationOperation.MUTATE,
                gene="hook_type",
                old_value="visual_question",
                new_value="cold_open",
                rationale="Test a faster hook.",
            ),
            Mutation(
                experiment=child,
                operation=MutationOperation.RECOMBINE,
                gene="music_style",
                old_value="soft_music_box",
                new_value="ukulele",
                source_experiment=donor,
                rationale="Borrow proven music gene.",
            ),
        ]
    )
    session.flush()
    session.expire_all()

    loaded = session.get_one(Experiment, child.id)
    assert {link.parent_id for link in loaded.parents} == {parent.id, donor.id}
    assert loaded.lineage_id == parent.lineage_id
    assert {m.gene for m in loaded.mutations} == {"hook_type", "music_style"}
    borrowed = next(m for m in loaded.mutations if m.gene == "music_style")
    assert borrowed.source_experiment_id == donor.id
    assert [link.experiment_id for link in session.get_one(Experiment, parent.id).children] == [
        child.id
    ]
    assert set(get_lineage(session, child.id).experiment.parent_experiment_ids) == {
        parent.id,
        donor.id,
    }


def test_an_experiment_cannot_be_its_own_parent(session: Session) -> None:
    experiment = make_experiment(session)
    session.add(
        ExperimentParent(experiment=experiment, parent=experiment, relation=ParentRelation.MUTATION)
    )

    with pytest.raises(IntegrityError):
        session.flush()


# --- accounts, publications, metrics, fitness ----------------------------


def test_an_ip_has_at_most_one_account_per_platform(session: Session) -> None:
    experiment = make_experiment(session)
    make_account(session, experiment)
    session.add(
        PlatformAccount(
            ip=experiment.ip, platform=Platform.YOUTUBE_SHORTS, external_account_id="UC999"
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_metric_snapshot_links_to_publication_experiment_and_account(session: Session) -> None:
    experiment = make_experiment(session)
    snapshot = make_metric(session, make_publication(session, experiment))
    session.expire_all()

    loaded = session.get_one(MetricSnapshot, snapshot.id)
    assert loaded.publication.experiment_id == experiment.id
    account = loaded.publication.platform_account
    assert account is not None
    assert account.ip_id == experiment.ip_id
    assert account.external_account_id == "UC123"
    assert loaded.raw == {"viewCount": "1200"}
    assert loaded.normalized == {"views": 1200}
    assert [s.id for s in loaded.publication.metric_snapshots] == [snapshot.id]


def test_video_fitness_references_its_metric_snapshot_and_formula(session: Session) -> None:
    experiment = make_experiment(session)
    publication = make_publication(session, experiment)
    metric = make_metric(session, publication)
    fitness = FitnessSnapshot(
        scope=FitnessScope.VIDEO,
        ip=experiment.ip,
        experiment=experiment,
        publication=publication,
        metric_snapshot=metric,
        platform=Platform.YOUTUBE_SHORTS,
        evaluator="heuristic",
        evaluator_version="1",
        score=0.62,
        components={"completion": 0.5},
        inputs={"views": 1200},
        checkpoint="72h",
    )
    session.add(fitness)
    session.flush()
    session.expire_all()

    loaded = session.get_one(FitnessSnapshot, fitness.id)
    assert loaded.metric_snapshot is not None
    assert loaded.metric_snapshot.publication_id == publication.id
    assert (loaded.evaluator, loaded.evaluator_version, loaded.score) == ("heuristic", "1", 0.62)
    assert [f.id for f in session.get_one(Experiment, experiment.id).fitness_snapshots] == [
        fitness.id
    ]


def test_ip_fitness_needs_no_experiment_but_video_fitness_does(session: Session) -> None:
    ip = make_experiment(session).ip
    session.add(
        FitnessSnapshot(
            scope=FitnessScope.IP,
            ip=ip,
            evaluator="ip-agg",
            evaluator_version="1",
            score=0.4,
            components={},
            inputs={},
        )
    )
    session.flush()
    session.add(
        FitnessSnapshot(
            scope=FitnessScope.VIDEO,
            ip=ip,
            evaluator="heuristic",
            evaluator_version="1",
            score=0.4,
            components={},
            inputs={},
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


# --- decisions and knowledge ---------------------------------------------


def test_selection_decision_records_reason_evidence_bucket_and_outcome(session: Session) -> None:
    parent, child = make_experiment(session), make_experiment(session)
    decision = SelectionDecision(
        decision_type=DecisionType.CREATE_EXPERIMENT,
        bucket=AllocationBucket.MUTATE,
        ip=parent.ip,
        subject_experiment=parent,
        resulting_experiment=child,
        reason="Completion uplift at 72h on YouTube; mutate hook.",
        evidence={"fitness_snapshot_ids": [], "uplift": 0.12},
        policy_version="alloc-1",
    )
    session.add(decision)
    session.flush()
    session.expire_all()

    loaded = session.scalars(select(SelectionDecision)).one()
    assert loaded.bucket is AllocationBucket.MUTATE
    assert loaded.subject_experiment_id == parent.id
    assert loaded.resulting_experiment_id == child.id
    assert loaded.evidence["uplift"] == 0.12


def test_knowledge_summary_references_supporting_experiments_and_can_be_revised(
    session: Session,
) -> None:
    first, second = make_experiment(session), make_experiment(session)
    summary = KnowledgeSummary(
        ip=first.ip,
        topic="hooks",
        statement="Question hooks improved 3-second retention but not completion.",
        confidence=0.6,
        supporting_experiments=[first, second],
    )
    session.add(summary)
    session.flush()

    summary.statement = "Question hooks improved retention and completion."
    summary.version += 1
    session.flush()
    session.expire_all()

    loaded = session.get_one(KnowledgeSummary, summary.id)
    assert loaded.version == 2
    assert {e.id for e in loaded.supporting_experiments} == {first.id, second.id}


# --- provider models and jobs --------------------------------------------


def test_provider_model_is_unique_per_provider_model_and_operation(session: Session) -> None:
    def model() -> ProviderModel:
        return ProviderModel(
            provider="higgsfield",
            model="alibaba/wan-3.0/text-to-video",
            operation="text_to_video",
            capabilities={"durations": [2, 30], "resolutions": ["480p", "720p"]},
            pricing={"usd_per_second": {"480p": "0.05"}},
        )

    session.add(model())
    session.flush()
    session.add(model())

    with pytest.raises(IntegrityError):
        session.flush()


def test_job_run_links_to_an_experiment_and_has_a_unique_idempotency_key(session: Session) -> None:
    experiment = make_experiment(session)
    job = JobRun(
        job_type="media_generation", experiment=experiment, idempotency_key="gen:1", payload={}
    )
    session.add(job)
    session.flush()
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert [j.id for j in session.get_one(Experiment, experiment.id).job_runs] == [job.id]

    session.add(JobRun(job_type="media_generation", idempotency_key="gen:1", payload={}))
    with pytest.raises(IntegrityError):
        session.flush()


# --- immutable evidence ---------------------------------------------------


def test_metric_snapshots_cannot_be_rewritten(session: Session) -> None:
    snapshot = make_metric(session, make_publication(session, make_experiment(session)))

    snapshot.normalized = {"views": 999_999}

    with pytest.raises(ImmutableEvidenceError):
        session.flush()


def test_evidence_rows_cannot_be_deleted(session: Session) -> None:
    experiment = make_experiment(session)
    session.flush()

    session.delete(experiment.hypothesis)

    with pytest.raises(ImmutableEvidenceError):
        session.flush()


@pytest.mark.parametrize("field", ["reason", "decision"])
def test_human_review_decisions_cannot_be_edited(session: Session, field: str) -> None:
    experiment = make_experiment(session)
    review = HumanReview(
        experiment=experiment,
        asset=make_asset(session, experiment),
        decision=ReviewDecision.REJECT,
        reason="Too dark.",
        reviewer="lior",
    )
    session.add(review)
    session.flush()

    setattr(review, field, ReviewDecision.APPROVE if field == "decision" else "Fine actually.")

    with pytest.raises(ImmutableEvidenceError):
        session.flush()


def test_genomes_and_fitness_history_are_immutable(session: Session) -> None:
    experiment = make_experiment(session)
    session.flush()

    experiment.genome.genes = {**experiment.genome.genes, "hook_type": "rewritten"}

    with pytest.raises(ImmutableEvidenceError):
        session.flush()


def test_ledger_actual_cost_can_be_settled_once_but_estimates_never_change(
    session: Session,
) -> None:
    from app.budgets.models import BudgetLedgerEntry

    entry = BudgetLedgerEntry(
        provider="fake", model="m", operation="text_to_video", estimated_cost_usd=Decimal("0.40")
    )
    session.add(entry)
    session.flush()

    entry.estimated_cost_usd = Decimal("0.01")

    with pytest.raises(ImmutableEvidenceError):
        session.flush()
