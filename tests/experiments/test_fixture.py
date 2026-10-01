import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import Experiment
from app.experiments.service import create_experiment
from app.experiments.spec import ExperimentSpec
from app.experiments.states import ExperimentStatus, VideoStatus
from app.ips.models import IP


def test_fixture_validates_against_the_domain_schema() -> None:
    reparsed = ExperimentSpec.model_validate(FIRST_SHORT.model_dump(mode="json"))

    assert reparsed == FIRST_SHORT


def test_fixture_targets_a_short_vertical_video_for_ages_four_to_eight() -> None:
    assert FIRST_SHORT.ip.spec["age_range"] == [4, 8]
    assert FIRST_SHORT.output.aspect_ratio == "9:16"
    genes = FIRST_SHORT.genome.genes
    assert FIRST_SHORT.output.min_duration_seconds <= genes.duration_seconds
    assert genes.duration_seconds <= FIRST_SHORT.output.max_duration_seconds


def test_spec_rejects_a_genome_duration_outside_the_output_requirements() -> None:
    data = FIRST_SHORT.model_dump(mode="json")
    data["genome"]["genes"]["duration_seconds"] = 600

    with pytest.raises(ValidationError, match="duration"):
        ExperimentSpec.model_validate(data)


def test_spec_rejects_unknown_genes() -> None:
    data = FIRST_SHORT.model_dump(mode="json")
    data["genome"]["genes"]["made_up_gene"] = "x"

    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(data)


def test_fixture_becomes_a_persisted_candidate_ready_for_production(session: Session) -> None:
    experiment = create_experiment(session, FIRST_SHORT)
    session.flush()
    session.expire_all()

    loaded = session.get_one(Experiment, experiment.id)
    assert loaded.status is ExperimentStatus.CANDIDATE_CREATED
    assert loaded.video_status is VideoStatus.PROPOSED
    assert loaded.ip.slug == FIRST_SHORT.ip.slug
    assert loaded.hypothesis.statement == FIRST_SHORT.hypothesis.statement
    assert loaded.genome.genes == FIRST_SHORT.genome.genes.model_dump(mode="json")
    assert loaded.genome.creative_spec == FIRST_SHORT.genome.creative_spec
    assert loaded.generation_reason == FIRST_SHORT.generation_reason
    assert loaded.output_requirements == FIRST_SHORT.output.model_dump(mode="json")


def test_creating_two_experiments_for_one_ip_reuses_the_ip(session: Session) -> None:
    first = create_experiment(session, FIRST_SHORT)
    second = create_experiment(session, FIRST_SHORT)
    session.flush()

    assert first.ip_id == second.ip_id
    assert first.lineage_id != second.lineage_id
    assert session.scalar(select(func.count()).select_from(IP)) == 1
