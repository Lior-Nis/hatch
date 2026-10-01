import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.creative.genome import (
    GENE_SPECS,
    GENOME_SCHEMA_VERSION,
    GeneGroup,
    GeneKind,
    Genes,
    Genome,
    UnsupportedGenomeVersion,
    diff_genes,
    genome_fingerprint,
    parse_genes,
)
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.queries import find_experiments_by_genes
from app.experiments.service import create_experiment

BASE = FIRST_SHORT.genome.genes


def variant(**changes: object) -> Genes:
    return Genes.model_validate({**BASE.model_dump(), **changes})


# --- typed genes and validation ------------------------------------------


def test_every_gene_is_classified_as_creative_or_production() -> None:
    assert set(GENE_SPECS) == set(Genes.model_fields)
    assert GENE_SPECS["hook_type"].group is GeneGroup.CREATIVE
    assert GENE_SPECS["video_model"].group is GeneGroup.PRODUCTION
    assert GENE_SPECS["hook_type"].kind is GeneKind.CATEGORICAL
    assert GENE_SPECS["duration_seconds"].kind is GeneKind.NUMERIC
    assert GENE_SPECS["topic"].kind is GeneKind.TEXT


@pytest.mark.parametrize("value", ["Visual Question", "visual-question", "", "visual_question!"])
def test_categorical_genes_must_be_normalised_tokens(value: str) -> None:
    with pytest.raises(ValidationError):
        variant(hook_type=value)


@pytest.mark.parametrize(
    ("gene", "value"),
    [("duration_seconds", 0), ("duration_seconds", 181), ("scene_count", 0), ("scene_count", 21)],
)
def test_numeric_genes_are_range_checked(gene: str, value: int) -> None:
    with pytest.raises(ValidationError):
        variant(**{gene: value})


def test_aspect_ratio_must_look_like_a_ratio() -> None:
    with pytest.raises(ValidationError):
        variant(aspect_ratio="vertical")


def test_a_new_categorical_value_is_allowed_so_novelty_can_invent_genes() -> None:
    assert variant(hook_type="mystery_box").hook_type == "mystery_box"


# --- versioning and serialisation ----------------------------------------


def test_genome_serialises_and_parses_back_identically() -> None:
    genome = FIRST_SHORT.genome
    data = genome.model_dump(mode="json")

    assert Genome.model_validate(data) == genome
    assert data["schema_version"] == GENOME_SCHEMA_VERSION


def test_stored_genes_are_parsed_by_their_schema_version() -> None:
    assert parse_genes(GENOME_SCHEMA_VERSION, BASE.model_dump(mode="json")) == BASE


def test_an_unknown_schema_version_is_refused_rather_than_guessed() -> None:
    with pytest.raises(UnsupportedGenomeVersion):
        parse_genes(GENOME_SCHEMA_VERSION + 1, BASE.model_dump(mode="json"))


def test_fingerprint_is_stable_and_changes_with_any_gene_or_the_spec() -> None:
    genome = FIRST_SHORT.genome
    same = Genome.model_validate(genome.model_dump(mode="json"))
    other_gene = genome.model_copy(update={"genes": variant(pace="brisk")})
    other_spec = genome.model_copy(update={"creative_spec": "A different story."})

    assert genome_fingerprint(genome) == genome_fingerprint(same)
    assert len({genome_fingerprint(g) for g in (genome, other_gene, other_spec)}) == 3


# --- field-by-field comparison -------------------------------------------


def test_identical_genes_have_no_differences() -> None:
    assert diff_genes(BASE, variant()) == []


def test_differences_list_each_changed_gene_with_old_and_new_values() -> None:
    changed = variant(hook_type="cold_open", duration_seconds=12, supporting_characters=("Moss",))

    differences = {d.gene: (d.old, d.new) for d in diff_genes(BASE, changed)}

    assert differences == {
        "hook_type": ("visual_question", "cold_open"),
        "duration_seconds": (8, 12),
        "supporting_characters": ((), ("Moss",)),
    }


def test_differences_report_the_gene_group() -> None:
    [difference] = diff_genes(BASE, variant(resolution="720p"))

    assert difference.group is GeneGroup.PRODUCTION


# --- queryable -----------------------------------------------------------


def test_experiments_can_be_found_by_gene_values(session: Session) -> None:
    question = create_experiment(session, FIRST_SHORT)
    cold_open = create_experiment(
        session,
        FIRST_SHORT.model_copy(
            update={
                "genome": FIRST_SHORT.genome.model_copy(
                    update={"genes": variant(hook_type="cold_open")}
                )
            }
        ),
    )

    def found(**genes: object) -> set[object]:
        return {experiment.id for experiment in find_experiments_by_genes(session, **genes)}

    assert found(hook_type="visual_question") == {question.id}
    assert found(hook_type="cold_open") == {cold_open.id}
    assert found(pace="gentle") == {question.id, cold_open.id}
    assert found(hook_type="cold_open", duration_seconds=8) == {cold_open.id}
    assert found(hook_type="never_used") == set()


def test_querying_an_unknown_gene_is_an_error(session: Session) -> None:
    with pytest.raises(KeyError):
        find_experiments_by_genes(session, not_a_gene="x")
