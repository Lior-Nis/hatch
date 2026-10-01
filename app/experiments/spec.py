"""The experiment specification: everything needed to start one candidate."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.creative.genome import Genome
from app.creative.hypotheses import Prediction
from app.ips.profile import IPProfile


class IPSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str
    category: str
    spec: dict[str, JsonValue]
    """An ``IPProfile`` as JSON."""

    @model_validator(mode="after")
    def _spec_is_a_profile(self) -> Self:
        IPProfile.model_validate(self.spec)
        return self


class HypothesisSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prediction: Prediction
    source: str


class OutputRequirements(BaseModel):
    """What the produced video must satisfy; technical QA checks against it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aspect_ratio: str = "9:16"
    min_duration_seconds: float = Field(gt=0)
    max_duration_seconds: float = Field(gt=0)
    min_height: int = Field(gt=0)
    audio_expected: bool = True


def check_genome_fits_output(genome: Genome, output: OutputRequirements) -> None:
    """Raise ``ValueError`` if the genome asks for a video the output
    requirements would reject."""
    duration = genome.genes.duration_seconds
    if not output.min_duration_seconds <= duration <= output.max_duration_seconds:
        raise ValueError(
            f"genome duration {duration}s is outside the output requirements "
            f"[{output.min_duration_seconds}, {output.max_duration_seconds}]"
        )
    if genome.genes.aspect_ratio != output.aspect_ratio:
        raise ValueError("genome aspect_ratio does not match the output requirements")


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ip: IPSpec
    hypothesis: HypothesisSpec
    genome: Genome
    output: OutputRequirements
    generation_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _genome_fits_output_requirements(self) -> Self:
        check_genome_fits_output(self.genome, self.output)
        return self
