import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.creative.genome import GENE_SPECS, parse_genes
from app.evolution.models import MutationOperation, ParentRelation
from app.evolution.mutation import create_mutant, create_recombinant
from app.experiments.lineage import get_lineage
from app.experiments.models import Experiment
from app.experiments.spec import HypothesisSpec
from app.experiments.states import ExperimentStatus, VideoStatus
from tests.factories import make_experiment

HYPOTHESIS = HypothesisSpec(
    statement="A cold open raises completion versus a visual question.",
    rationale="Parent completed well; test whether the hook mechanism matters.",
    prediction={"metric": "completion_rate", "direction": "increase", "compared_to": "parent"},
    source="test",
)


def genes_of(experiment: Experiment) -> dict[str, object]:
    return parse_genes(experiment.genome.schema_version, experiment.genome.genes).model_dump()


def mutate(session: Session, parent: Experiment, **changes: object) -> Experiment:
    return create_mutant(
        session,
        parent,
        changes=changes,
        hypothesis=HYPOTHESIS,
        rationale="Test the hook mechanism.",
    )


def test_only_the_selected_genes_change(session: Session) -> None:
    parent = make_experiment(session)
    before = genes_of(parent)

    child = mutate(session, parent, hook_type="cold_open", duration_seconds=10)

    after = genes_of(child)
    changed = {gene for gene in GENE_SPECS if after[gene] != before[gene]}
    assert changed == {"hook_type", "duration_seconds"}
    assert (after["hook_type"], after["duration_seconds"]) == ("cold_open", 10)


def test_parent_genome_is_untouched(session: Session) -> None:
    parent = make_experiment(session)
    before = dict(parent.genome.genes)

    child = mutate(session, parent, hook_type="cold_open")

    session.expire_all()
    assert session.get_one(Experiment, parent.id).genome.genes == before
    assert child.genome_id != parent.genome_id


def test_each_changed_gene_is_recorded_with_old_and_new_values(session: Session) -> None:
    parent = make_experiment(session)

    child = mutate(session, parent, hook_type="cold_open", duration_seconds=10)

    recorded = {m.gene: (m.operation, m.old_value, m.new_value) for m in child.mutations}
    assert recorded == {
        "hook_type": (MutationOperation.MUTATE, "visual_question", "cold_open"),
        "duration_seconds": (MutationOperation.MUTATE, 8, 10),
    }
    assert all(m.rationale == "Test the hook mechanism." for m in child.mutations)


def test_descendant_preserves_lineage_ip_and_requirements(session: Session) -> None:
    parent = make_experiment(session)

    child = mutate(session, parent, hook_type="cold_open")

    assert child.lineage_id == parent.lineage_id
    assert child.ip_id == parent.ip_id
    assert child.output_requirements == parent.output_requirements
    assert child.status is ExperimentStatus.CANDIDATE_CREATED
    assert child.video_status is VideoStatus.PROPOSED
    [link] = child.parents
    assert (link.parent_id, link.relation) == (parent.id, ParentRelation.MUTATION)
    assert child.hypothesis.statement == HYPOTHESIS.statement
    assert str(parent.id) in child.generation_reason
    assert get_lineage(session, child.id).experiment.parent_experiment_ids == [parent.id]


def test_creative_spec_is_inherited_unless_a_new_one_is_given(session: Session) -> None:
    parent = make_experiment(session)

    inherited = mutate(session, parent, hook_type="cold_open")
    rewritten = create_mutant(
        session,
        parent,
        changes={"hook_type": "cold_open"},
        hypothesis=HYPOTHESIS,
        rationale="r",
        creative_spec="Open mid-action: Nib is already parting the fern.",
    )

    assert inherited.genome.creative_spec == parent.genome.creative_spec
    assert rewritten.genome.creative_spec.startswith("Open mid-action")


def test_a_mutation_must_change_something(session: Session) -> None:
    parent = make_experiment(session)

    with pytest.raises(ValueError, match="no gene"):
        mutate(session, parent)
    with pytest.raises(ValueError, match="no gene"):
        mutate(session, parent, hook_type="visual_question")  # same as the parent


def test_unknown_or_invalid_gene_values_are_rejected(session: Session) -> None:
    parent = make_experiment(session)

    with pytest.raises(KeyError):
        mutate(session, parent, made_up_gene="x")
    with pytest.raises(ValidationError):
        mutate(session, parent, hook_type="Not A Token")


def test_a_mutation_that_violates_the_output_requirements_is_rejected(session: Session) -> None:
    parent = make_experiment(session)

    with pytest.raises(ValueError, match="duration"):
        mutate(session, parent, duration_seconds=120)


# --- recombination --------------------------------------------------------


def test_recombinant_borrows_named_genes_from_another_lineage(session: Session) -> None:
    parent = make_experiment(session)
    donor = mutate(session, make_experiment(session), music_style="ukulele", pace="brisk")
    before = genes_of(parent)

    child = create_recombinant(
        session,
        parent,
        borrow={"music_style": donor},
        hypothesis=HYPOTHESIS,
        rationale="Ukulele beds completed better in the donor lineage.",
    )

    after = genes_of(child)
    assert after["music_style"] == "ukulele"
    assert after["pace"] == before["pace"]  # not borrowed, so unchanged
    assert {g for g in GENE_SPECS if after[g] != before[g]} == {"music_style"}


def test_recombination_records_source_experiments_and_borrowed_genes(session: Session) -> None:
    parent = make_experiment(session)
    music_donor = mutate(session, make_experiment(session), music_style="ukulele")
    pace_donor = mutate(session, make_experiment(session), pace="brisk")

    child = create_recombinant(
        session,
        parent,
        borrow={"music_style": music_donor, "pace": pace_donor},
        hypothesis=HYPOTHESIS,
        rationale="Combine two proven genes.",
    )

    recorded = {m.gene: (m.operation, m.new_value, m.source_experiment_id) for m in child.mutations}
    assert recorded == {
        "music_style": (MutationOperation.RECOMBINE, "ukulele", music_donor.id),
        "pace": (MutationOperation.RECOMBINE, "brisk", pace_donor.id),
    }
    relations = {link.parent_id: link.relation for link in child.parents}
    assert relations == {
        parent.id: ParentRelation.MUTATION,
        music_donor.id: ParentRelation.RECOMBINATION,
        pace_donor.id: ParentRelation.RECOMBINATION,
    }
    assert child.lineage_id == parent.lineage_id


def test_borrowing_a_gene_the_parent_already_has_is_rejected(session: Session) -> None:
    parent = make_experiment(session)
    twin = make_experiment(session)

    with pytest.raises(ValueError, match="no gene"):
        create_recombinant(
            session, parent, borrow={"pace": twin}, hypothesis=HYPOTHESIS, rationale="r"
        )
