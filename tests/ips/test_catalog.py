from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.characters.models import Character, CharacterVersion
from app.creative.hypotheses import Prediction
from app.evolution.anti_cloning import external_ip_matches
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Hypothesis
from app.ips.catalog import INITIAL_IPS, seed_initial_ips
from app.ips.models import IP
from app.ips.profile import IPProfile
from app.ips.states import IPStatus
from app.production.prompting import build_video_prompt


def test_portfolio_has_one_ip_per_required_category() -> None:
    assert sorted(definition.ip.category for definition in INITIAL_IPS) == [
        "humor_imagination",
        "learning_problem_solving",
        "narrative_adventure",
    ]
    assert len({definition.ip.slug for definition in INITIAL_IPS}) == 3


def test_every_ip_has_a_structured_profile_for_ages_four_to_eight() -> None:
    for definition in INITIAL_IPS:
        profile = IPProfile.model_validate(definition.ip.spec)

        assert profile.age_range == (4, 8)
        assert profile.premise and profile.visual_identity and profile.tone
        assert len(profile.world_rules) >= 3
        assert len(profile.safety_constraints) >= 3
        assert profile.differentiation
        assert profile.characters, definition.ip.slug
        assert all(character.appearance for character in profile.characters)


def test_ips_are_clearly_differentiated() -> None:
    profiles = [IPProfile.model_validate(d.ip.spec) for d in INITIAL_IPS]

    assert len({profile.visual_identity for profile in profiles}) == 3
    names = [character.name for profile in profiles for character in profile.characters]
    assert len(names) == len(set(names))


def test_every_ip_starts_with_a_set_of_falsifiable_hypotheses() -> None:
    for definition in INITIAL_IPS:
        assert len(definition.initial_hypotheses) >= 3
        for hypothesis in definition.initial_hypotheses:
            assert isinstance(hypothesis.prediction, Prediction)
            assert hypothesis.prediction.compared_to == "ip_baseline"
            assert hypothesis.rationale
        tested = {h.prediction.genes_under_test for h in definition.initial_hypotheses}
        assert len(tested) >= 3  # the starting hypotheses test different mechanisms


def test_no_ip_borrows_from_well_known_properties() -> None:
    for definition in INITIAL_IPS:
        text = definition.ip.name + " " + definition.ip.model_dump_json()
        assert external_ip_matches(text) == ()


def test_seeding_creates_ips_characters_and_hypotheses_once(session: Session) -> None:
    seed_initial_ips(session)
    seed_initial_ips(session)

    ips = session.scalars(select(IP)).all()
    assert {ip.slug for ip in ips} == {d.ip.slug for d in INITIAL_IPS}
    assert all(ip.status is IPStatus.IDEA for ip in ips)
    expected_characters = sum(
        len(IPProfile.model_validate(d.ip.spec).characters) for d in INITIAL_IPS
    )
    assert session.scalar(select(func.count()).select_from(Character)) == expected_characters
    assert session.scalar(select(func.count()).select_from(CharacterVersion)) == expected_characters
    expected_hypotheses = sum(len(d.initial_hypotheses) for d in INITIAL_IPS)
    assert session.scalar(select(func.count()).select_from(Hypothesis)) == expected_hypotheses
    sources = set(session.scalars(select(Hypothesis.source)))
    assert sources == {"initial_portfolio"}


def test_fixture_uses_the_catalog_definition_of_its_ip() -> None:
    catalog = next(d for d in INITIAL_IPS if d.ip.slug == FIRST_SHORT.ip.slug)

    assert FIRST_SHORT.ip == catalog.ip


def test_video_prompt_describes_the_lead_character_for_visual_consistency() -> None:
    prompt = build_video_prompt(
        ip_spec=dict(FIRST_SHORT.ip.spec),
        genes=FIRST_SHORT.genome.genes,
        creative_spec=FIRST_SHORT.genome.creative_spec,
    )
    nib = next(
        c for c in IPProfile.model_validate(FIRST_SHORT.ip.spec).characters if c.name == "Nib"
    )

    assert nib.appearance in prompt
