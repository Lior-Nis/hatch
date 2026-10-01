"""Creating experiments from specifications."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.experiments.models import CreativeGenome, Experiment, Hypothesis
from app.experiments.spec import ExperimentSpec, IPSpec
from app.experiments.states import ExperimentStatus
from app.ips.models import IP


def get_or_create_ip(session: Session, spec: IPSpec) -> IP:
    ip = session.scalars(select(IP).where(IP.slug == spec.slug)).one_or_none()
    if ip is None:
        ip = IP(slug=spec.slug, name=spec.name, category=spec.category, spec=dict(spec.spec))
        session.add(ip)
        session.flush()
    return ip


def create_experiment(session: Session, spec: ExperimentSpec) -> Experiment:
    """Persist a parentless candidate (hypothesis + genome + experiment)."""
    ip = get_or_create_ip(session, spec.ip)
    hypothesis = Hypothesis(
        ip=ip,
        statement=spec.hypothesis.statement,
        rationale=spec.hypothesis.rationale,
        prediction=dict(spec.hypothesis.prediction),
        source=spec.hypothesis.source,
    )
    genome = CreativeGenome(
        schema_version=spec.genome.schema_version,
        genes=spec.genome.genes.model_dump(mode="json"),
        creative_spec=spec.genome.creative_spec,
    )
    experiment = Experiment(
        ip=ip,
        hypothesis=hypothesis,
        genome=genome,
        generation_reason=spec.generation_reason,
        output_requirements=spec.output.model_dump(mode="json"),
        status=ExperimentStatus.CANDIDATE_CREATED,
    )
    session.add(experiment)
    session.flush()
    return experiment
