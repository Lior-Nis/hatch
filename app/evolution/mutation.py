"""Controlled descendants: mutation and gene recombination.

Both operators copy a parent's genome, change only the named genes, and record
exactly what changed. V1 deliberately has no whole-parent crossover:
recombination borrows individual, named genes from other lineages.

The parent is never modified — genomes are immutable evidence.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.creative.genome import GENE_SPECS, Genes, Genome, diff_genes, parse_genes
from app.evolution.models import ExperimentParent, Mutation, MutationOperation, ParentRelation
from app.experiments.models import Experiment
from app.experiments.service import persist_candidate
from app.experiments.spec import HypothesisSpec, OutputRequirements


def _genes(experiment: Experiment) -> Genes:
    return parse_genes(experiment.genome.schema_version, experiment.genome.genes)


def _descend(
    session: Session,
    parent: Experiment,
    *,
    changes: Mapping[str, Any],
    sources: Mapping[str, Experiment],
    hypothesis: HypothesisSpec,
    rationale: str,
    creative_spec: str | None,
    reason: str,
) -> Experiment:
    unknown = set(changes) - set(GENE_SPECS)
    if unknown:
        raise KeyError(f"unknown genes: {', '.join(sorted(unknown))}")
    parent_genes = _genes(parent)
    child_genes = Genes.model_validate({**parent_genes.model_dump(), **changes})
    differences = diff_genes(parent_genes, child_genes)
    if not differences:
        raise ValueError("no gene would change: a descendant must differ from its parent")

    child = persist_candidate(
        session,
        ip=parent.ip,
        hypothesis=hypothesis,
        genome=Genome(
            genes=child_genes, creative_spec=creative_spec or parent.genome.creative_spec
        ),
        output=OutputRequirements.model_validate(parent.output_requirements),
        generation_reason=reason,
        lineage_id=parent.lineage_id,
    )
    session.add(ExperimentParent(experiment=child, parent=parent, relation=ParentRelation.MUTATION))
    changed_genes = {difference.gene for difference in differences}
    donors = {source.id: source for gene, source in sources.items() if gene in changed_genes}
    for donor in donors.values():
        session.add(
            ExperimentParent(experiment=child, parent=donor, relation=ParentRelation.RECOMBINATION)
        )
    as_json = child_genes.model_dump(mode="json")
    parent_json = parent_genes.model_dump(mode="json")
    for difference in differences:
        source = sources.get(difference.gene)
        session.add(
            Mutation(
                experiment=child,
                operation=MutationOperation.RECOMBINE if source else MutationOperation.MUTATE,
                gene=difference.gene,
                old_value=parent_json[difference.gene],
                new_value=as_json[difference.gene],
                source_experiment=source,
                rationale=rationale,
            )
        )
    session.flush()
    return child


def create_mutant(
    session: Session,
    parent: Experiment,
    *,
    changes: Mapping[str, Any],
    hypothesis: HypothesisSpec,
    rationale: str,
    creative_spec: str | None = None,
) -> Experiment:
    """A descendant of ``parent`` with only the genes in ``changes`` altered."""
    changed = ", ".join(sorted(changes)) or "nothing"
    return _descend(
        session,
        parent,
        changes=changes,
        sources={},
        hypothesis=hypothesis,
        rationale=rationale,
        creative_spec=creative_spec,
        reason=f"Mutation of experiment {parent.id} ({changed}): {rationale}",
    )


def create_recombinant(
    session: Session,
    parent: Experiment,
    *,
    borrow: Mapping[str, Experiment],
    hypothesis: HypothesisSpec,
    rationale: str,
    creative_spec: str | None = None,
) -> Experiment:
    """A descendant of ``parent`` that takes each gene named in ``borrow`` from
    the given donor experiment (typically a proven gene from another lineage)."""
    changes = {gene: getattr(_genes(donor), gene) for gene, donor in borrow.items()}
    borrowed = ", ".join(f"{gene} from {donor.id}" for gene, donor in sorted(borrow.items()))
    return _descend(
        session,
        parent,
        changes=changes,
        sources=dict(borrow),
        hypothesis=hypothesis,
        rationale=rationale,
        creative_spec=creative_spec,
        reason=f"Recombination on experiment {parent.id} ({borrowed}): {rationale}",
    )
