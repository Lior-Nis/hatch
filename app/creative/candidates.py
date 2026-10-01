"""Creative agent: proposes candidates as explicit, falsifiable experiments.

Two entry points:

- ``propose_mutation`` — a controlled descendant of a parent experiment.
- ``propose_novel``    — a parentless candidate for an IP (novel exploration).

The language model drafts; Hatch decides. Every draft is validated against the
genome schema and the experiment rules, and an invalid draft is sent back once
with the exact problem. Nothing is persisted unless a draft passes.
"""

import json
import uuid
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
)
from app.evolution.mutation import create_mutant
from app.experiments.models import Experiment
from app.experiments.service import persist_candidate
from app.experiments.spec import HypothesisSpec, OutputRequirements, check_genome_fits_output
from app.ips.models import IP
from app.llm.calls import attribute_to_experiment, call_model
from app.llm.ports import LanguageModel, LLMRequest

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
    sections = [
        "## The IP",
        _json(describe_ip(ip)),
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


def propose_mutation(
    session: Session, parent: Experiment, *, llm: LanguageModel, governor: BudgetGovernor
) -> Experiment:
    """Ask the creative agent for a controlled descendant of ``parent`` and
    persist it with its hypothesis, mutations and lineage."""
    parent_genes = parse_genes(parent.genome.schema_version, parent.genome.genes)
    context, known = _context(session, parent.ip)
    prompt = "\n\n".join(
        [
            context,
            "## The parent experiment",
            _json(describe_experiment(parent)),
            "## Your task",
            f"Propose a controlled descendant of experiment {parent.id}. Change between 1 and "
            f"{MAX_MUTATED_GENES} creative genes and return every other creative gene exactly "
            "as the parent has it. Rewrite creative_spec so it matches the changed genes. "
            "The hypothesis compares this video against its parent, so genes_under_test must "
            "name only genes you changed.",
        ]
    )

    def accept(draft: CandidateDraft) -> Experiment:
        values = draft.creative_genes.model_dump()
        child_genes = Genes.model_validate({**parent_genes.model_dump(), **values})
        changed = [difference.gene for difference in diff_genes(parent_genes, child_genes)]
        if not changed:
            raise ValueError("no gene changed: a descendant must differ from its parent")
        if len(changed) > MAX_MUTATED_GENES:
            raise ValueError(
                f"change at most {MAX_MUTATED_GENES} genes so the experiment stays controlled "
                f"(you changed {len(changed)}: {', '.join(changed)})"
            )
        if not draft.genes_under_test or not set(draft.genes_under_test) <= set(changed):
            raise ValueError(
                f"genes_under_test must name only genes you changed ({', '.join(changed)})"
            )
        spec = draft.creative_spec.strip()
        if not spec or spec == parent.genome.creative_spec.strip():
            raise ValueError("creative_spec must be rewritten to reflect the changed genes")
        hypothesis = HypothesisSpec(
            statement=draft.hypothesis_statement,
            rationale=draft.rationale,
            prediction=_prediction(draft, "parent", known),
            source=f"creative_agent:{llm.model}",
        )
        return create_mutant(
            session,
            parent,
            changes={gene: getattr(child_genes, gene) for gene in changed},
            hypothesis=hypothesis,
            rationale=draft.rationale,
            creative_spec=spec,
        )

    child, call_ids = _draft_until_valid(
        session, llm, governor, purpose="hypothesis_mutation", prompt=prompt, accept=accept
    )
    attribute_to_experiment(session, call_ids, child)
    session.commit()
    assert isinstance(child, Experiment)
    return child


def propose_novel(
    session: Session,
    ip: IP,
    *,
    llm: LanguageModel,
    governor: BudgetGovernor,
    output: OutputRequirements,
    production: ProductionDefaults,
) -> Experiment:
    """Ask the creative agent for a parentless candidate: a new idea for this
    IP that does not extend any existing experiment."""
    context, known = _context(session, ip)
    existing = [parse_genes(e.genome.schema_version, e.genome.genes) for e in known]
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
        genes = Genes.model_validate(
            {**draft.creative_genes.model_dump(), **production.model_dump()}
        )
        if any(not diff_genes(genes, other) for other in existing):
            raise ValueError(
                "a candidate with exactly these genes already exists in this IP; propose "
                "something new"
            )
        if not draft.creative_spec.strip():
            raise ValueError("creative_spec is empty")
        genome = Genome(genes=genes, creative_spec=draft.creative_spec.strip())
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
        )

    candidate, call_ids = _draft_until_valid(
        session, llm, governor, purpose="hypothesis_novel", prompt=prompt, accept=accept
    )
    attribute_to_experiment(session, call_ids, candidate)
    session.commit()
    assert isinstance(candidate, Experiment)
    return candidate
