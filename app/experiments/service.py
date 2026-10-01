"""Creating experiments."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.genome import Genome
from app.experiments.models import CreativeGenome, Experiment, Hypothesis
from app.experiments.spec import (
    ExperimentSpec,
    HypothesisSpec,
    IPSpec,
    OutputRequirements,
    check_genome_fits_output,
)
from app.experiments.states import ExperimentStatus
from app.ips.models import IP


def get_or_create_ip(session: Session, spec: IPSpec) -> IP:
    ip = session.scalars(select(IP).where(IP.slug == spec.slug)).one_or_none()
    if ip is None:
        ip = IP(slug=spec.slug, name=spec.name, category=spec.category, spec=dict(spec.spec))
        session.add(ip)
        session.flush()
    return ip


def persist_candidate(
    session: Session,
    *,
    ip: IP,
    hypothesis: HypothesisSpec,
    genome: Genome,
    output: OutputRequirements,
    generation_reason: str,
    lineage_id: uuid.UUID | None = None,
) -> Experiment:
    """Persist hypothesis + genome + experiment as one candidate. A descendant
    passes its ancestor's ``lineage_id``; a novel candidate starts a new one."""
    check_genome_fits_output(genome, output)
    experiment = Experiment(
        ip=ip,
        hypothesis=Hypothesis(
            ip=ip,
            statement=hypothesis.statement,
            rationale=hypothesis.rationale,
            prediction=dict(hypothesis.prediction),
            source=hypothesis.source,
        ),
        genome=CreativeGenome(
            schema_version=genome.schema_version,
            genes=genome.genes.model_dump(mode="json"),
            creative_spec=genome.creative_spec,
        ),
        generation_reason=generation_reason,
        output_requirements=output.model_dump(mode="json"),
        lineage_id=lineage_id or uuid.uuid4(),
    )
    session.add(experiment)
    # Hypothesis and genome exist, so the candidate is fully specified.
    experiment.status = ExperimentStatus.CANDIDATE_CREATED
    session.flush()
    return experiment


def create_experiment(
    session: Session, spec: ExperimentSpec, *, lineage_id: uuid.UUID | None = None
) -> Experiment:
    """Persist a full specification, creating its IP if it does not exist."""
    return persist_candidate(
        session,
        ip=get_or_create_ip(session, spec.ip),
        hypothesis=spec.hypothesis,
        genome=spec.genome,
        output=spec.output,
        generation_reason=spec.generation_reason,
        lineage_id=lineage_id,
    )
