"""Experiment memory as creative agents see it.

Builds compact, factual context from the database: what the IP is, what has
been tried, how it went, and what is currently believed. Read-only.
"""

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.creative.genome import GENE_SPECS, GeneGroup, GeneKind
from app.experiments.models import Experiment, Hypothesis
from app.fitness.models import FitnessScope
from app.ips.models import IP
from app.knowledge.models import KnowledgeSummary


def creative_genes_of(experiment: Experiment) -> dict[str, Any]:
    return {
        name: value
        for name, value in experiment.genome.genes.items()
        if name in GENE_SPECS and GENE_SPECS[name].group is GeneGroup.CREATIVE
    }


def describe_ip(ip: IP) -> dict[str, Any]:
    return {"name": ip.name, "category": ip.category, "status": ip.status.value, **ip.spec}


def describe_experiment(experiment: Experiment, *, with_spec: bool = True) -> dict[str, Any]:
    description: dict[str, Any] = {
        "experiment_id": str(experiment.id),
        "hypothesis": experiment.hypothesis.statement,
        "creative_genes": creative_genes_of(experiment),
        "video_status": experiment.video_status.value,
        "conclusion": experiment.conclusion.value if experiment.conclusion else None,
        "fitness": {
            snapshot.platform.value: round(snapshot.score, 3)
            for snapshot in experiment.fitness_snapshots
            if snapshot.scope is FitnessScope.VIDEO and snapshot.platform is not None
        },
    }
    if with_spec:
        description["creative_spec"] = experiment.genome.creative_spec
    return description


def ip_experiments(session: Session, ip: IP, *, limit: int = 30) -> list[Experiment]:
    """The IP's most recent experiments, newest first."""
    return list(
        session.scalars(
            select(Experiment)
            .where(Experiment.ip_id == ip.id)
            .options(
                selectinload(Experiment.genome),
                selectinload(Experiment.hypothesis),
                selectinload(Experiment.fitness_snapshots),
            )
            .order_by(Experiment.created_at.desc())
            .limit(limit)
        )
    )


def gene_usage(experiments: list[Experiment]) -> dict[str, dict[str, int]]:
    """How often each categorical creative gene value has been used."""
    usage: dict[str, Counter[str]] = {}
    for experiment in experiments:
        for name, value in creative_genes_of(experiment).items():
            if GENE_SPECS[name].kind is GeneKind.CATEGORICAL:
                usage.setdefault(name, Counter())[str(value)] += 1
    return {name: dict(counter.most_common()) for name, counter in sorted(usage.items())}


def knowledge_for(session: Session, ip: IP) -> list[dict[str, Any]]:
    summaries = session.scalars(
        select(KnowledgeSummary)
        .where((KnowledgeSummary.ip_id == ip.id) | (KnowledgeSummary.ip_id.is_(None)))
        .order_by(KnowledgeSummary.updated_at.desc())
        .limit(20)
    )
    return [
        {"topic": s.topic, "statement": s.statement, "confidence": s.confidence} for s in summaries
    ]


def untested_starting_hypotheses(session: Session, ip: IP) -> list[dict[str, Any]]:
    """The IP's seeded hypotheses that no experiment has taken up yet."""
    tested = set(
        session.scalars(
            select(Hypothesis.statement)
            .join(Experiment, Experiment.hypothesis_id == Hypothesis.id)
            .where(Experiment.ip_id == ip.id)
        )
    )
    return [
        {"statement": h.statement, "rationale": h.rationale, "prediction": h.prediction}
        for h in session.scalars(
            select(Hypothesis)
            .where(Hypothesis.ip_id == ip.id, Hypothesis.source == "initial_portfolio")
            .order_by(Hypothesis.created_at)
        )
        if h.statement not in tested
    ]
