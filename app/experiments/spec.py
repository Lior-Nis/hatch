"""The experiment specification: everything needed to start one candidate."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.creative.genome import Genome


class IPSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str
    category: str
    spec: dict[str, JsonValue]


class HypothesisSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    prediction: dict[str, JsonValue]
    source: str


class OutputRequirements(BaseModel):
    """What the produced video must satisfy; technical QA checks against it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aspect_ratio: str = "9:16"
    min_duration_seconds: float = Field(gt=0)
    max_duration_seconds: float = Field(gt=0)
    min_height: int = Field(gt=0)
    audio_expected: bool = True


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ip: IPSpec
    hypothesis: HypothesisSpec
    genome: Genome
    output: OutputRequirements
    generation_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _genome_fits_output_requirements(self) -> Self:
        duration = self.genome.genes.duration_seconds
        if not self.output.min_duration_seconds <= duration <= self.output.max_duration_seconds:
            raise ValueError(
                f"genome duration {duration}s is outside the output requirements "
                f"[{self.output.min_duration_seconds}, {self.output.max_duration_seconds}]"
            )
        if self.genome.genes.aspect_ratio != self.output.aspect_ratio:
            raise ValueError("genome aspect_ratio does not match the output requirements")
        return self
