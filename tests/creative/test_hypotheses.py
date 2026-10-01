from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.candidates import (
    CandidateDraft,
    InvalidProposal,
    ProductionDefaults,
    propose_exploit,
    propose_mutation,
    propose_novel,
    routed_production,
)
from app.creative.genome import GENE_SPECS, parse_genes
from app.creative.hypotheses import Metric, Prediction
from app.creative.memory import creative_genes_of
from app.evolution.models import ExperimentParent, ParentRelation
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.llm.models import ModelCall
from app.llm.ports import LLMRequest
from app.production.catalog import seed_provider_models
from integrations.fake.llm import FakeLanguageModel
from tests.factories import LIMITS, make_experiment

BASE = FIRST_SHORT.genome.genes
DEFAULTS = ProductionDefaults(
    video_model="alibaba/wan-3.0/text-to-video", resolution="480p", prompt_strategy="single_shot_v1"
)


def creative_genes(**changes: Any) -> dict[str, Any]:
    creative = {
        name: value
        for name, value in BASE.model_dump().items()
        if GENE_SPECS[name].group.value == "creative"
    }
    return {**creative, **changes}


NEW_STORY = {"topic": "how rain makes music", "educational_goal": "rain makes sounds"}
RAIN_SPEC = (
    "Morning rain taps the roof of the hollow. Nib sets out three acorn cups and listens: "
    "each one sings a different note as the drops fall. Nib arranges them into a tune."
)


def draft(**overrides: Any) -> CandidateDraft:
    fields: dict[str, Any] = {
        "creative_genes": creative_genes(hook_type="cold_open", **NEW_STORY),
        "creative_spec": RAIN_SPEC,
        "hypothesis_statement": "A cold open raises completion versus a visual question hook.",
        "rationale": "The parent's reveal lands late; starting inside the action removes the wait.",
        "metric": "completion_rate",
        "direction": "increase",
        "minimum_relative_effect": 0.1,
        "genes_under_test": ["hook_type"],
        "evidence_experiment_ids": [],
    }
    fields.update(overrides)
    return CandidateDraft.model_validate(fields)


def mechanism_mutations(experiment: Experiment) -> list[str]:
    return sorted(m.gene for m in experiment.mutations if GENE_SPECS[m.gene].mechanism)


def governor() -> BudgetGovernor:
    return BudgetGovernor(LIMITS)


def mutate(
    session: Session, parent: Experiment, *responses: BaseModel | Callable[..., Any]
) -> Experiment:
    llm = FakeLanguageModel(list(responses), cost_usd=Decimal("0.02"))
    return propose_mutation(session, parent, llm=llm, governor=governor())


# --- the machine-readable hypothesis --------------------------------------


def test_prediction_names_metric_direction_baseline_effect_and_genes() -> None:
    prediction = Prediction(
        metric=Metric.COMPLETION_RATE,
        direction="increase",
        compared_to="parent",
        minimum_relative_effect=0.1,
        genes_under_test=("hook_type",),
    )

    assert prediction.model_dump(mode="json")["metric"] == "completion_rate"


@pytest.mark.parametrize(
    "bad",
    [
        {"genes_under_test": ()},
        {"genes_under_test": ("not_a_gene",)},
        {"minimum_relative_effect": 0},
        {"metric": "vibes"},
        {"direction": "sideways"},
    ],
)
def test_a_prediction_that_cannot_be_falsified_is_rejected(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "metric": "completion_rate",
        "direction": "increase",
        "compared_to": "parent",
        "minimum_relative_effect": 0.1,
        "genes_under_test": ("hook_type",),
    }
    with pytest.raises(ValidationError):
        Prediction.model_validate({**fields, **bad})


# --- mutation proposals ----------------------------------------------------


def test_generated_candidate_references_a_machine_readable_hypothesis(session: Session) -> None:
    parent = make_experiment(session)

    child = mutate(session, parent, draft())

    prediction = Prediction.model_validate(child.hypothesis.prediction)
    assert prediction.metric is Metric.COMPLETION_RATE
    assert prediction.compared_to == "parent"
    assert prediction.genes_under_test == ("hook_type",)
    assert prediction.minimum_relative_effect == 0.1
    assert child.hypothesis.statement.startswith("A cold open raises completion")
    assert child.hypothesis.rationale.startswith("The parent's reveal lands late")
    assert child.hypothesis.source == "creative_agent:fake-llm-1"


def test_proposed_mutation_becomes_a_controlled_descendant(session: Session) -> None:
    parent = make_experiment(session)

    child = mutate(session, parent, draft())

    genes = parse_genes(child.genome.schema_version, child.genome.genes)
    assert genes.hook_type == "cold_open"
    assert genes.video_model == BASE.video_model  # production genes are inherited
    assert mechanism_mutations(child) == ["hook_type"]
    assert child.lineage_id == parent.lineage_id
    assert child.genome.creative_spec == RAIN_SPEC
    assert [link.relation for link in child.parents] == [ParentRelation.MUTATION]


def test_the_model_sees_ip_context_parent_and_experiment_memory(session: Session) -> None:
    parent = make_experiment(session)
    seen: list[LLMRequest] = []

    def respond(request: LLMRequest) -> CandidateDraft:
        seen.append(request)
        return draft()

    mutate(session, parent, respond)

    [request] = seen
    assert request.purpose == "hypothesis_mutation"
    assert "ages 4" in request.system or "aged 4" in request.system
    assert "No scary imagery" in request.prompt  # IP safety constraints
    assert "visual_question" in request.prompt  # the parent's genes
    assert parent.genome.creative_spec in request.prompt
    assert str(parent.id) in request.prompt


def test_the_model_call_and_its_cost_are_attributed_to_the_new_candidate(session: Session) -> None:
    parent = make_experiment(session)

    child = mutate(session, parent, draft())

    call = session.scalars(select(ModelCall)).one()
    assert call.experiment_id == child.id
    assert call.purpose == "hypothesis_mutation"
    assert governor().committed_spend_for_experiment(session, child.id) == Decimal("0.02")


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        ({"creative_genes": creative_genes(**NEW_STORY)}, "no mechanism gene"),
        ({"genes_under_test": ["pace"]}, "genes_under_test"),
        ({"genes_under_test": ["topic"]}, "genes_under_test"),
        (
            {
                "creative_genes": creative_genes(
                    hook_type="cold_open",
                    pace="brisk",
                    music_style="ukulele",
                    ending_type="twist",
                    **NEW_STORY,
                )
            },
            "at most",
        ),
        ({"creative_genes": creative_genes(hook_type="Cold Open!", **NEW_STORY)}, "hook_type"),
        (
            {
                "creative_genes": creative_genes(hook_type="cold_open", topic="a glowing fern"),
                "creative_spec": FIRST_SHORT.genome.creative_spec,
            },
            "too similar",
        ),
        ({"creative_genes": creative_genes(hook_type="cold_open")}, "its own topic"),
        ({"creative_spec": "Nib meets Peppa Pig. " + RAIN_SPEC}, "Peppa Pig"),
    ],
)
def test_an_invalid_proposal_is_sent_back_with_the_problem_then_accepted(
    session: Session, bad: dict[str, Any], problem: str
) -> None:
    parent = make_experiment(session)
    llm = FakeLanguageModel([draft(**bad), draft()])

    child = propose_mutation(session, parent, llm=llm, governor=governor())

    assert len(llm.requests) == 2
    assert problem in llm.requests[1].prompt
    assert mechanism_mutations(child) == ["hook_type"]
    assert len(session.scalars(select(ModelCall)).all()) == 2


def test_repeatedly_invalid_proposals_create_no_experiment(session: Session) -> None:
    parent = make_experiment(session)
    bad = draft(creative_genes=creative_genes(**NEW_STORY))
    llm = FakeLanguageModel([bad, bad, bad])

    with pytest.raises(InvalidProposal):
        propose_mutation(session, parent, llm=llm, governor=governor())

    assert len(session.scalars(select(Experiment)).all()) == 1
    assert len(llm.requests) == 2


# --- novelty ---------------------------------------------------------------


def novel_draft(**overrides: Any) -> CandidateDraft:
    fields: dict[str, Any] = {
        "creative_genes": creative_genes(
            topic="why moss feels soft",
            story_archetype="gentle_experiment",
            hook_type="sound_first",
            visual_style="paper_cutout",
            camera_style="top_down",
            music_style="kalimba",
        ),
        "creative_spec": "Nib presses a paw into a moss cushion; it springs back with a boing.",
        "hypothesis_statement": "A sound-first hook with a tactile topic beats the IP baseline.",
        "rationale": "Nothing in this IP has tried touch or sound as the entry point.",
        "genes_under_test": ["hook_type", "topic"],
    }
    fields.update(overrides)
    return draft(**fields)


def test_novel_candidate_is_created_without_any_parent(session: Session) -> None:
    ip = make_experiment(session).ip
    llm = FakeLanguageModel([novel_draft()])

    candidate = propose_novel(
        session,
        ip,
        llm=llm,
        governor=governor(),
        output=FIRST_SHORT.output,
        production=DEFAULTS,
    )

    assert session.scalars(select(ExperimentParent)).all() == []
    assert candidate.mutations == []
    assert candidate.ip_id == ip.id
    assert candidate.lineage_id not in {
        e.lineage_id for e in session.scalars(select(Experiment)) if e.id != candidate.id
    }
    genes = parse_genes(candidate.genome.schema_version, candidate.genome.genes)
    assert genes.hook_type == "sound_first"
    assert (genes.video_model, genes.resolution) == (DEFAULTS.video_model, "480p")
    prediction = Prediction.model_validate(candidate.hypothesis.prediction)
    assert prediction.compared_to == "ip_baseline"
    assert candidate.generation_reason.startswith("Novel exploration")
    assert llm.requests[0].purpose == "hypothesis_novel"


def test_novelty_prompt_shows_what_has_been_tried_so_it_can_avoid_it(session: Session) -> None:
    existing = make_experiment(session)
    llm = FakeLanguageModel([novel_draft()])

    propose_novel(
        session, existing.ip, llm=llm, governor=governor(), output=FIRST_SHORT.output,
        production=DEFAULTS,
    )  # fmt: skip

    prompt = llm.requests[0].prompt
    assert "visual_question" in prompt  # gene values already used in this IP
    assert "tiny_mystery" in prompt


def test_a_novel_proposal_that_retells_an_existing_story_is_sent_back(session: Session) -> None:
    existing = make_experiment(session)
    copy = novel_draft(
        creative_genes=creative_genes(),
        creative_spec=FIRST_SHORT.genome.creative_spec,
        genes_under_test=["hook_type"],
    )
    llm = FakeLanguageModel([copy, novel_draft()])

    candidate = propose_novel(
        session, existing.ip, llm=llm, governor=governor(), output=FIRST_SHORT.output,
        production=DEFAULTS,
    )  # fmt: skip

    assert len(llm.requests) == 2
    assert "too similar" in llm.requests[1].prompt
    assert candidate.genome.genes["hook_type"] == "sound_first"


# --- exploitation: same mechanism, new story --------------------------------


def exploit_draft(**overrides: Any) -> CandidateDraft:
    fields: dict[str, Any] = {"creative_genes": creative_genes(**NEW_STORY)}
    fields.update(overrides)
    return draft(**fields)


def test_exploit_keeps_every_mechanism_gene_and_tells_a_new_story(session: Session) -> None:
    parent = make_experiment(session)
    llm = FakeLanguageModel([exploit_draft()])

    child = propose_exploit(session, parent, llm=llm, governor=governor())

    assert mechanism_mutations(child) == []
    assert {m.gene for m in child.mutations} == {"topic", "educational_goal"}
    assert child.genome.creative_spec == RAIN_SPEC
    assert child.lineage_id == parent.lineage_id
    assert [link.relation for link in child.parents] == [ParentRelation.EXPLOIT]
    assert llm.requests[0].purpose == "hypothesis_exploit"


def test_exploit_retests_the_lineage_hypothesis_rather_than_inventing_one(session: Session) -> None:
    parent = make_experiment(session)
    llm = FakeLanguageModel([exploit_draft(metric="shares_per_view", genes_under_test=["pace"])])

    child = propose_exploit(session, parent, llm=llm, governor=governor())

    inherited = Prediction.model_validate(parent.hypothesis.prediction)
    prediction = Prediction.model_validate(child.hypothesis.prediction)
    assert prediction.metric is inherited.metric
    assert prediction.genes_under_test == inherited.genes_under_test
    assert prediction.minimum_relative_effect == inherited.minimum_relative_effect
    assert prediction.compared_to == "ip_baseline"


def test_replication_descendants_are_marked_as_such(session: Session) -> None:
    parent = make_experiment(session)
    llm = FakeLanguageModel([exploit_draft()])

    child = propose_exploit(
        session, parent, llm=llm, governor=governor(), relation=ParentRelation.REPLICATION
    )

    assert [link.relation for link in child.parents] == [ParentRelation.REPLICATION]
    assert "Replication" in child.generation_reason


def test_an_exploit_draft_that_changes_the_mechanism_is_sent_back(session: Session) -> None:
    parent = make_experiment(session)
    llm = FakeLanguageModel(
        [exploit_draft(creative_genes=creative_genes(pace="brisk", **NEW_STORY)), exploit_draft()]
    )

    child = propose_exploit(session, parent, llm=llm, governor=governor())

    assert "pace" in llm.requests[1].prompt
    assert mechanism_mutations(child) == []


def test_two_descendants_of_one_parent_cannot_tell_the_same_story(session: Session) -> None:
    parent = make_experiment(session)
    propose_exploit(session, parent, llm=FakeLanguageModel([exploit_draft()]), governor=governor())
    fresh = exploit_draft(
        creative_genes=creative_genes(topic="why snails leave silver trails"),
        creative_spec="A snail glides over a leaf at dawn, leaving a shining path Nib follows.",
    )
    llm = FakeLanguageModel([exploit_draft(), fresh])

    sibling = propose_exploit(session, parent, llm=llm, governor=governor())

    assert "too similar" in llm.requests[1].prompt
    assert sibling.genome.genes["topic"] == "why snails leave silver trails"


# --- model routing for parentless candidates --------------------------------


def test_novel_candidate_gets_its_model_from_the_router_and_keeps_the_decision(
    session: Session,
) -> None:
    ip = make_experiment(session).ip
    seed_provider_models(session)
    planner = routed_production(
        target_cost_usd=Decimal("0.50"),
        max_cost_usd=Decimal("0.75"),
        prompt_strategy="single_shot_v1",
    )

    candidate = propose_novel(
        session, ip, llm=FakeLanguageModel([novel_draft()]), governor=governor(),
        output=FIRST_SHORT.output, production=planner,
    )  # fmt: skip

    genes = parse_genes(candidate.genome.schema_version, candidate.genome.genes)
    assert (genes.video_model, genes.resolution) == ("alibaba/wan-3.0/text-to-video", "480p")
    routing = candidate.production_plan["routing"]
    assert routing["model"] == genes.video_model
    assert "target" in routing["reason"]
    assert routing["fallbacks"]
    assert any(c["rejected_because"] for c in routing["considered"])


def test_routing_override_from_config_is_applied_and_recorded(session: Session) -> None:
    ip = make_experiment(session).ip
    seed_provider_models(session)
    planner = routed_production(
        target_cost_usd=Decimal("0.50"),
        max_cost_usd=Decimal("0.75"),
        prompt_strategy="single_shot_v1",
        override="lightricks/ltx-2.5/text-to-video/fast",
    )

    candidate = propose_novel(
        session, ip, llm=FakeLanguageModel([novel_draft()]), governor=governor(),
        output=FIRST_SHORT.output, production=planner,
    )  # fmt: skip

    assert candidate.genome.genes["video_model"] == "lightricks/ltx-2.5/text-to-video/fast"
    assert "override" in candidate.production_plan["routing"]["reason"]


def test_descendants_inherit_their_parents_production_plan(session: Session) -> None:
    ip = make_experiment(session).ip
    seed_provider_models(session)
    planner = routed_production(
        target_cost_usd=Decimal("0.50"),
        max_cost_usd=Decimal("0.75"),
        prompt_strategy="single_shot_v1",
    )
    parent = propose_novel(
        session, ip, llm=FakeLanguageModel([novel_draft()]), governor=governor(),
        output=FIRST_SHORT.output, production=planner,
    )  # fmt: skip
    child_draft = draft(
        creative_genes={**creative_genes_of(parent), "hook_type": "cold_open", **NEW_STORY}
    )

    child = propose_mutation(
        session, parent, llm=FakeLanguageModel([child_draft]), governor=governor()
    )

    assert child.production_plan == parent.production_plan
    assert child.genome.genes["video_model"] == parent.genome.genes["video_model"]
