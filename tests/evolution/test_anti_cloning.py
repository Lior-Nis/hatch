import pytest
from sqlalchemy.orm import Session

from app.creative.genome import GENE_SPECS, Genes
from app.evolution.anti_cloning import (
    AntiCloningPolicy,
    check_clone,
    external_ip_matches,
    lexical_similarity,
)
from app.experiments.fixtures import FIRST_SHORT
from tests.factories import make_experiment

BASE = FIRST_SHORT.genome.genes
SPEC = FIRST_SHORT.genome.creative_spec
OTHER_STORY = (
    "Morning rain taps the roof of the hollow. Nib sets out three acorn cups and listens: "
    "each one sings a different note as the drops fall. Nib arranges them into a tune."
)


def genes(**changes: object) -> Genes:
    return Genes.model_validate({**BASE.model_dump(), **changes})


# --- which genes are surface ----------------------------------------------


def test_surface_genes_are_the_story_content_not_the_mechanism() -> None:
    surface = {name for name, spec in GENE_SPECS.items() if spec.surface}

    assert surface == {"topic", "educational_goal", "supporting_characters"}
    assert not GENE_SPECS["hook_type"].surface


# --- similarity ------------------------------------------------------------


def test_identical_text_is_fully_similar_and_unrelated_text_is_not() -> None:
    assert lexical_similarity(SPEC, SPEC) == 1.0
    assert lexical_similarity(SPEC, "Seven blue boats race across a sunny harbour.") < 0.1


def test_similarity_ignores_case_punctuation_and_filler_words() -> None:
    assert lexical_similarity("Nib parts the fern!", "the nib PARTS a fern") == 1.0


# --- clone check -----------------------------------------------------------


def test_an_identical_story_is_blocked_and_names_the_experiment_it_copies(session: Session) -> None:
    original = make_experiment(session)

    verdict = check_clone(session, ip=original.ip, genes=BASE, creative_spec=SPEC)

    assert verdict.blocked
    assert verdict.nearest_experiment_id == original.id
    assert verdict.nearest_similarity == 1.0
    assert "too similar" in verdict.reasons[0]


def test_a_reworded_near_duplicate_is_blocked(session: Session) -> None:
    original = make_experiment(session)
    reworded = SPEC.replace("tiptoes toward", "creeps toward").replace("delight", "joy")

    verdict = check_clone(
        session, ip=original.ip, genes=genes(hook_type="cold_open"), creative_spec=reworded
    )

    assert verdict.blocked
    assert verdict.nearest_similarity > 0.6


def test_the_similarity_threshold_is_configurable(session: Session) -> None:
    original = make_experiment(session)
    reworded = SPEC.replace("tiptoes toward", "creeps toward").replace("delight", "joy")

    lenient = check_clone(
        session,
        ip=original.ip,
        genes=BASE,
        creative_spec=reworded,
        policy=AntiCloningPolicy(max_surface_similarity=0.99),
    )

    assert not lenient.blocked


def test_a_different_story_with_the_same_mechanism_is_allowed(session: Session) -> None:
    original = make_experiment(session)

    verdict = check_clone(
        session,
        ip=original.ip,
        genes=genes(topic="how rain makes music", educational_goal="rain makes sounds"),
        creative_spec=OTHER_STORY,
    )

    assert not verdict.blocked
    assert verdict.nearest_experiment_id == original.id
    assert verdict.nearest_similarity < 0.6


def test_an_empty_catalog_never_blocks(session: Session) -> None:
    ip = make_experiment(session, ip_slug="empty-ip").ip
    other_ip_story = make_experiment(session)  # same story, but in another IP

    verdict = check_clone(
        session, ip=ip, genes=BASE, creative_spec=OTHER_STORY, exclude={other_ip_story.id}
    )

    assert not verdict.blocked


def test_an_experiment_can_be_excluded_from_the_comparison(session: Session) -> None:
    original = make_experiment(session)

    verdict = check_clone(
        session, ip=original.ip, genes=BASE, creative_spec=SPEC, exclude={original.id}
    )

    assert not verdict.blocked


# --- external IP -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("Nib meets Peppa Pig at the pond.", "Peppa Pig"),
        ("A pup in a PAW PATROL vest helps.", "Paw Patrol"),
        ("Nib builds a tower of lego bricks.", "LEGO"),
        ("A yellow sponge like SpongeBob waves.", "SpongeBob"),
    ],
)
def test_well_known_external_properties_are_detected(text: str, match: str) -> None:
    assert match in external_ip_matches(text)


@pytest.mark.parametrize(
    "text", ["Nib looks elsewhere for the glow.", "A blue bird sings.", "Moss marionettes dance."]
)
def test_ordinary_words_are_not_mistaken_for_brands(text: str) -> None:
    assert external_ip_matches(text) == ()


def test_resemblance_to_external_ip_blocks_the_candidate(session: Session) -> None:
    ip = make_experiment(session).ip

    verdict = check_clone(
        session,
        ip=ip,
        genes=genes(topic="a visit from a famous pig"),
        creative_spec="Peppa Pig jumps in muddy puddles with Nib. " + OTHER_STORY,
    )

    assert verdict.blocked
    assert verdict.external_ip_matches == ("Peppa Pig",)
    assert any("Peppa Pig" in reason for reason in verdict.reasons)
