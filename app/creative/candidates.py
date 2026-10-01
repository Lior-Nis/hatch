"""Creative agent: proposes candidates as explicit, falsifiable experiments.

Three entry points, one per allocation bucket:

- ``propose_exploit``  — same mechanism as a proven parent, new story.
- ``propose_mutation`` — 1–3 mechanism genes changed, new story.
- ``propose_novel``    — a parentless candidate for an IP (novel exploration).

Every candidate must tell a story the IP has not told (anti-cloning): Hatch
evolves mechanisms, it does not copy surfaces.

The language model drafts; Hatch decides. Every draft is validated against the
genome schema and the experiment rules, and an invalid draft is sent back once
with the exact problem. Nothing is persisted unless a draft passes.
"""

import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.genome import (
    GENE_SPECS,
    GeneGroup,
    GeneKind,
    Genes,
    Genome,
    diff_genes,
    parse_genes,
)
from app.creative.hypotheses import Prediction
from app.creative.memory import (
    describe_experiment,
    describe_ip,
    gene_usage,
    ip_experiments,
    knowledge_for,
    untested_starting_hypotheses,
)
from app.evolution.anti_cloning import AntiCloningPolicy, check_clone
from app.evolution.models import ParentRelation
from app.evolution.mutation import create_mutant
from app.experiments.models import Experiment
from app.experiments.service import persist_candidate
from app.experiments.spec import HypothesisSpec, OutputRequirements, check_genome_fits_output
from app.ips.models import IP
from app.llm.calls import attribute_to_experiment, call_model
from app.llm.ports import LanguageModel, LLMRequest
from app.observability.health import provider_health
from app.production.routing import (
    NoEligibleModel,
    RoutingDecision,
    RoutingRequirements,
    load_catalog,
    route_video_model,
)

MAX_MUTATED_GENES = 3
MAX_DRAFT_ATTEMPTS = 2

_PLAIN_TYPES: dict[GeneKind, Any] = {
    GeneKind.CATEGORICAL: str,
    GeneKind.TEXT: str,
    GeneKind.NUMERIC: int,
    GeneKind.LIST: list[str],
}


def _creative_gene_values_model() -> type[BaseModel]:
    """The creative genes with plain types. Deliberately lenient: Hatch's own
    genome validation (not the model's output schema) decides what is valid, so
    a near-miss can be explained and corrected instead of failing the call."""
    fields: dict[str, Any] = {}
    for name, spec in GENE_SPECS.items():
        if spec.group is not GeneGroup.CREATIVE:
            continue
        plain = _PLAIN_TYPES[spec.kind]
        optional = not Genes.model_fields[name].is_required() and spec.kind is GeneKind.TEXT
        fields[name] = (plain | None, None) if optional else (plain, ...)
    return create_model("CreativeGeneValues", __config__=ConfigDict(extra="forbid"), **fields)


if TYPE_CHECKING:
    CreativeGeneValues = BaseModel  # the real model is built from the genome schema
else:
    CreativeGeneValues = _creative_gene_values_model()


class CandidateDraft(BaseModel):
    """What the language model returns."""

    model_config = ConfigDict(extra="forbid")

    creative_genes: CreativeGeneValues
    creative_spec: str
    hypothesis_statement: str
    rationale: str
    metric: str
    direction: str
    minimum_relative_effect: float
    genes_under_test: list[str]
    evidence_experiment_ids: list[str] = Field(default_factory=list)


class ProductionDefaults(BaseModel):
    """Production genes for parentless candidates (creative agents do not
    choose how a video is made)."""

    model_config = ConfigDict(frozen=True)

    video_model: str
    resolution: str
    prompt_strategy: str
    image_model: str | None = None
    aspect_ratio: str = "9:16"


class ProductionChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    genes: ProductionDefaults
    routing: RoutingDecision | None = None


Planner = Callable[[Session, int, str, bool], ProductionChoice]
"""(session, duration_seconds, aspect_ratio, with_audio) → production genes."""


def routed_production(
    *,
    target_cost_usd: Decimal,
    max_cost_usd: Decimal,
    prompt_strategy: str,
    override: str | None = None,
) -> Planner:
    """A planner that routes each candidate to a model from the catalogue,
    taking recent provider reliability into account."""

    def plan(
        session: Session, duration_seconds: int, aspect_ratio: str, with_audio: bool
    ) -> ProductionChoice:
        failure_rates = {
            health.model: health.failure_rate
            for health in provider_health(session)
            if health.failure_rate is not None
        }
        decision = route_video_model(
            RoutingRequirements(
                duration_seconds=duration_seconds,
                aspect_ratio=aspect_ratio,
                with_audio=with_audio,
                target_cost_usd=target_cost_usd,
                max_cost_usd=max_cost_usd,
            ),
            load_catalog(session),
            failure_rates=failure_rates,
            override=override,
        )
        return ProductionChoice(
            genes=ProductionDefaults(
                video_model=decision.model,
                resolution=decision.resolution,
                prompt_strategy=prompt_strategy,
                aspect_ratio=aspect_ratio,
            ),
            routing=decision,
        )

    return plan


class InvalidProposal(Exception):
    """The model's drafts kept violating the experiment rules."""


_SYSTEM = """\
You are the creative research agent of Hatch, a studio that makes original short \
vertical videos for children aged 4 to 8 and learns what works from audience evidence.

You propose one candidate video at a time, as an experiment that tests a hypothesis.

Safety is absolute and comes before engagement: gentle and warm, never frightening, no \
peril, no violence, nothing a child could imitate unsafely, no adult themes, no \
manipulation, and no resemblance to existing characters, brands or logos. Stay inside the \
IP's world rules and safety constraints.

Creative genes are of two kinds. Mechanism genes (hook_type, story_archetype, pace, \
visual_style, music_style, ending_type and so on) are the reusable structure that \
hypotheses are about. Surface genes (topic, educational_goal, supporting_characters) \
and the creative_spec are what one particular video is about. Every candidate must tell \
a story this IP has not told before: a new topic and new events, never a retelling.

How to fill the draft:
- Categorical gene values are lowercase snake_case tokens such as visual_question or \
soft_3d_storybook. You may invent a new token when nothing existing fits.
- creative_spec is the shot description a video model will follow: concrete, visual, \
present tense, consistent with every gene.
- The hypothesis must be falsifiable. State what you expect and why in plain words, then \
name the metric, the direction, the smallest relative effect that would count \
(0.1 means 10%), and the genes whose change you claim causes it.
- metric is one of: completion_rate, average_watch_fraction, average_watch_seconds, \
views_per_impression, likes_per_view, shares_per_view, saves_per_view, follows_per_view.
- direction is increase or decrease.
- evidence_experiment_ids lists the earlier experiments whose results informed you \
(empty when there is no evidence yet).
"""


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _context(session: Session, ip: IP) -> tuple[str, list[Experiment]]:
    experiments = ip_experiments(session, ip)
    untested = untested_starting_hypotheses(session, ip)
    sections = [
        "## The IP",
        _json(describe_ip(ip)),
        "## Starting hypotheses for this IP that no experiment has tested yet",
        _json(untested) if untested else "None left.",
        "## What has been tried in this IP (newest first)",
        _json([describe_experiment(e, with_spec=False) for e in experiments]),
        "## Gene values used so far (value: times used)",
        _json(gene_usage(experiments)),
        "## What we currently believe",
        _json(knowledge_for(session, ip)) if knowledge_for(session, ip) else "No evidence yet.",
    ]
    return "\n\n".join(sections), experiments


def _evidence_ids(draft: CandidateDraft, known: list[Experiment]) -> tuple[uuid.UUID, ...]:
    known_ids = {str(experiment.id): experiment.id for experiment in known}
    return tuple(known_ids[i] for i in draft.evidence_experiment_ids if i in known_ids)


def _prediction(
    draft: CandidateDraft,
    compared_to: Literal["parent", "ip_baseline"],
    known: list[Experiment],
) -> Prediction:
    return Prediction.model_validate(
        {
            "metric": draft.metric,
            "direction": draft.direction,
            "compared_to": compared_to,
            "minimum_relative_effect": draft.minimum_relative_effect,
            "genes_under_test": draft.genes_under_test,
            "evidence_experiment_ids": _evidence_ids(draft, known),
        }
    )


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or 'draft'}: {issue['msg']}"
        for issue in error.errors()
    )


def _draft_until_valid(
    session: Session,
    llm: LanguageModel,
    governor: BudgetGovernor,
    *,
    purpose: str,
    prompt: str,
    accept: Any,
) -> tuple[Any, list[uuid.UUID]]:
    """Ask for a draft; if Hatch rejects it, ask once more with the reason.
    Returns whatever ``accept`` builds from the first valid draft, plus the ids
    of every model call made."""
    call_ids: list[uuid.UUID] = []
    feedback = ""
    problem = "no draft was produced"
    for _ in range(MAX_DRAFT_ATTEMPTS):
        request = LLMRequest(purpose=purpose, system=_SYSTEM, prompt=prompt + feedback)
        result = call_model(session, llm, governor, request, CandidateDraft, recorded=call_ids)
        try:
            return accept(result.parsed), call_ids
        except ValidationError as exc:
            problem = _problems(exc)
        except (ValueError, KeyError) as exc:
            problem = str(exc)
        feedback = (
            "\n\n## Your previous draft was rejected\n"
            f"{problem}\nSend a corrected draft that fixes exactly this."
        )
    raise InvalidProposal(problem)


def _new_story(
    session: Session, ip: IP, genes: Genes, creative_spec: str, policy: AntiCloningPolicy
) -> str:
    """The candidate's creative spec, or ``ValueError`` if it clones the catalogue."""
    spec = creative_spec.strip()
    if not spec:
        raise ValueError("creative_spec is empty")
    verdict = check_clone(session, ip=ip, genes=genes, creative_spec=spec, policy=policy)
    if verdict.blocked:
        raise ValueError("; ".join(verdict.reasons))
    return spec


def _propose_descendant(
    session: Session,
    parent: Experiment,
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    policy: AntiCloningPolicy,
    purpose: str,
    task: str,
    relation: ParentRelation,
    mechanism_changes: tuple[int, int],
    inherit_prediction: bool,
) -> Experiment:
    parent_genes = parse_genes(parent.genome.schema_version, parent.genome.genes)
    context, known = _context(session, parent.ip)
    prompt = "\n\n".join(
        [
            context,
            "## The parent experiment",
            _json(describe_experiment(parent)),
            "## Your task",
            task,
        ]
    )
    fewest, most = mechanism_changes

    def accept(draft: CandidateDraft) -> Experiment:
        values = draft.creative_genes.model_dump()
        child_genes = Genes.model_validate({**parent_genes.model_dump(), **values})
        changed = [difference.gene for difference in diff_genes(parent_genes, child_genes)]
        mechanism = [gene for gene in changed if GENE_SPECS[gene].mechanism]
        if most == 0 and mechanism:
            raise ValueError(
                "keep every mechanism gene exactly as the parent has it; you changed "
                f"{', '.join(mechanism)}. Only the story (topic, educational_goal, "
                "supporting_characters, creative_spec) may change"
            )
        if len(mechanism) < fewest:
            raise ValueError(
                "no mechanism gene changed: a mutation must change at least one of the "
                "mechanism genes (hook_type, story_archetype, pace, ...)"
            )
        if len(mechanism) > most:
            raise ValueError(
                f"change at most {most} mechanism genes so the experiment stays controlled "
                f"(you changed {len(mechanism)}: {', '.join(mechanism)})"
            )
        if "topic" not in changed:
            raise ValueError("give the new story its own topic: it must differ from the parent's")
        spec = _new_story(session, parent.ip, child_genes, draft.creative_spec, policy)
        if inherit_prediction:
            # Re-test the lineage's claim on a new story instead of inventing one.
            inherited = Prediction.model_validate(parent.hypothesis.prediction)
            prediction = inherited.model_copy(
                update={
                    "compared_to": "ip_baseline",
                    "evidence_experiment_ids": (parent.id, *_evidence_ids(draft, known)),
                }
            )
        else:
            if not draft.genes_under_test or not set(draft.genes_under_test) <= set(mechanism):
                raise ValueError(
                    "genes_under_test must name only mechanism genes you changed "
                    f"({', '.join(mechanism)})"
                )
            prediction = _prediction(draft, "parent", known)
        return create_mutant(
            session,
            parent,
            changes={gene: getattr(child_genes, gene) for gene in changed},
            hypothesis=HypothesisSpec(
                statement=draft.hypothesis_statement,
                rationale=draft.rationale,
                prediction=prediction,
                source=f"creative_agent:{llm.model}",
            ),
            rationale=draft.rationale,
            creative_spec=spec,
            relation=relation,
        )

    child, call_ids = _draft_until_valid(
        session, llm, governor, purpose=purpose, prompt=prompt, accept=accept
    )
    attribute_to_experiment(session, call_ids, child)
    session.commit()
    assert isinstance(child, Experiment)
    return child


def propose_mutation(
    session: Session,
    parent: Experiment,
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    policy: AntiCloningPolicy | None = None,
) -> Experiment:
    """A controlled descendant: 1–3 mechanism genes changed, tested against
    the parent, told through a new story."""
    return _propose_descendant(
        session,
        parent,
        llm=llm,
        governor=governor,
        policy=policy or AntiCloningPolicy(),
        purpose="hypothesis_mutation",
        task=(
            f"Propose a controlled descendant of experiment {parent.id}. Change between 1 and "
            f"{MAX_MUTATED_GENES} mechanism genes and return every other mechanism gene exactly "
            "as the parent has it. Tell a new story (new topic, new creative_spec) that carries "
            "the changed mechanism. The hypothesis compares this video against its parent, so "
            "genes_under_test must name only mechanism genes you changed."
        ),
        relation=ParentRelation.MUTATION,
        mechanism_changes=(1, MAX_MUTATED_GENES),
        inherit_prediction=False,
    )


def propose_exploit(
    session: Session,
    parent: Experiment,
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    policy: AntiCloningPolicy | None = None,
    relation: ParentRelation = ParentRelation.EXPLOIT,
) -> Experiment:
    """Another video in a proven lineage: the parent's mechanism, unchanged,
    carried by a new story. With ``relation=REPLICATION`` it is one of the
    controlled descendants that test whether a potential winner reproduces."""
    return _propose_descendant(
        session,
        parent,
        llm=llm,
        governor=governor,
        policy=policy or AntiCloningPolicy(),
        purpose="hypothesis_exploit",
        task=(
            f"Propose another video in the lineage of experiment {parent.id}. Keep every "
            "mechanism gene exactly as the parent has it: the point is to learn whether this "
            "mechanism works again with different content. Tell a new story (new topic, new "
            "creative_spec, optionally a new educational_goal or supporting cast). In "
            "hypothesis_statement, say why this mechanism should work for the new story."
        ),
        relation=relation,
        mechanism_changes=(0, 0),
        inherit_prediction=True,
    )


def propose_novel(
    session: Session,
    ip: IP,
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    output: OutputRequirements,
    production: ProductionDefaults | Planner,
    policy: AntiCloningPolicy | None = None,
) -> Experiment:
    """Ask the creative agent for a parentless candidate: a new idea for this
    IP that does not extend any existing experiment."""
    context, known = _context(session, ip)
    policy = policy or AntiCloningPolicy()
    prompt = "\n\n".join(
        [
            context,
            "## Your task",
            "Propose a genuinely new candidate for this IP: a character moment, hook, story "
            "structure, educational angle or visual style that nothing above has tried. Do "
            "not extend or tweak an existing experiment. The video must run between "
            f"{output.min_duration_seconds:g} and {output.max_duration_seconds:g} seconds. "
            "The hypothesis compares this video against the IP's baseline; genes_under_test "
            "names the genes that carry the new idea.",
        ]
    )

    def accept(draft: CandidateDraft) -> Experiment:
        values = draft.creative_genes.model_dump()
        if isinstance(production, ProductionDefaults):
            choice = ProductionChoice(genes=production)
        else:
            try:
                choice = production(
                    session,
                    int(values["duration_seconds"]),
                    output.aspect_ratio,
                    output.audio_expected,
                )
            except NoEligibleModel as exc:
                raise ValueError(f"{exc}; choose a duration a model can produce") from exc
        genes = Genes.model_validate({**values, **choice.genes.model_dump()})
        spec = _new_story(session, ip, genes, draft.creative_spec, policy)
        genome = Genome(genes=genes, creative_spec=spec)
        check_genome_fits_output(genome, output)
        return persist_candidate(
            session,
            ip=ip,
            hypothesis=HypothesisSpec(
                statement=draft.hypothesis_statement,
                rationale=draft.rationale,
                prediction=_prediction(draft, "ip_baseline", known),
                source=f"creative_agent:{llm.model}",
            ),
            genome=genome,
            output=output,
            generation_reason=f"Novel exploration: {draft.rationale}",
            production_plan=(
                {"routing": choice.routing.model_dump(mode="json")} if choice.routing else None
            ),
        )

    candidate, call_ids = _draft_until_valid(
        session, llm, governor, purpose="hypothesis_novel", prompt=prompt, accept=accept
    )
    attribute_to_experiment(session, call_ids, candidate)
    session.commit()
    assert isinstance(candidate, Experiment)
    return candidate
