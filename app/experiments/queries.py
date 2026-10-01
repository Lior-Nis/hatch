"""Queries over experiment memory."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.genome import GENE_SPECS
from app.experiments.models import CreativeGenome, Experiment


def find_experiments_by_genes(session: Session, **genes: Any) -> list[Experiment]:
    """Experiments whose genome has all the given gene values (JSONB
    containment, served by the GIN index on ``creative_genomes.genes``)."""
    unknown = set(genes) - set(GENE_SPECS)
    if unknown:
        raise KeyError(f"unknown genes: {', '.join(sorted(unknown))}")
    return list(
        session.scalars(
            select(Experiment)
            .join(CreativeGenome, CreativeGenome.id == Experiment.genome_id)
            .where(CreativeGenome.genes.contains(genes))
            .order_by(Experiment.created_at)
        )
    )
