"""Creative genome, schema version 1.

A genome is the structured, queryable part of a candidate: creative genes
(what the video is) plus production genes (how it is made). The free-form
creative specification travels alongside it.

Design rules:
- Categorical genes are normalised tokens (``visual_question``), never prose,
  so they can be grouped, compared and mutated. The vocabulary is open: novelty
  may introduce a new token, but it must still be a token.
- Unknown genes are rejected, so a typo can never become an unanalysable gene.
- Stored genomes are parsed by their ``schema_version``; an unknown version is
  refused rather than guessed at.
"""

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

GENOME_SCHEMA_VERSION = 1


class GeneGroup(StrEnum):
    CREATIVE = "creative"
    PRODUCTION = "production"


class GeneKind(StrEnum):
    CATEGORICAL = "categorical"
    NUMERIC = "numeric"
    TEXT = "text"
    LIST = "list"


Token = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(_[a-z0-9]+)*$", max_length=60)]
"""A normalised categorical value."""
Text = Annotated[str, StringConstraints(min_length=1, max_length=300)]
Ratio = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*:[1-9][0-9]*$")]


def _gene(group: GeneGroup, kind: GeneKind, **field: Any) -> Any:
    return Field(json_schema_extra={"group": group.value, "kind": kind.value}, **field)


def _creative(kind: GeneKind, **field: Any) -> Any:
    return _gene(GeneGroup.CREATIVE, kind, **field)


def _production(kind: GeneKind, **field: Any) -> Any:
    return _gene(GeneGroup.PRODUCTION, kind, **field)


class Genes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- creative genes: what the video is ---
    primary_character: Text = _creative(GeneKind.TEXT)
    supporting_characters: tuple[Text, ...] = _creative(GeneKind.LIST, default=())
    topic: Text = _creative(GeneKind.TEXT)
    story_archetype: Token = _creative(GeneKind.CATEGORICAL)
    educational_goal: Text | None = _creative(GeneKind.TEXT, default=None)
    hook_type: Token = _creative(GeneKind.CATEGORICAL)
    dominant_emotion: Token = _creative(GeneKind.CATEGORICAL)
    visual_style: Token = _creative(GeneKind.CATEGORICAL)
    camera_style: Token = _creative(GeneKind.CATEGORICAL)
    duration_seconds: int = _creative(GeneKind.NUMERIC, ge=1, le=180)
    scene_count: int = _creative(GeneKind.NUMERIC, ge=1, le=20)
    pace: Token = _creative(GeneKind.CATEGORICAL)
    narrator_style: Token = _creative(GeneKind.CATEGORICAL)
    dialogue_density: Token = _creative(GeneKind.CATEGORICAL)
    music_style: Token = _creative(GeneKind.CATEGORICAL)
    ending_type: Token = _creative(GeneKind.CATEGORICAL)
    cta_type: Token = _creative(GeneKind.CATEGORICAL)

    # --- production genes: how it is made ---
    video_model: Text = _production(GeneKind.CATEGORICAL)
    image_model: Text | None = _production(GeneKind.CATEGORICAL, default=None)
    prompt_strategy: Token = _production(GeneKind.CATEGORICAL)
    resolution: Token = _production(GeneKind.CATEGORICAL)
    aspect_ratio: Ratio = _production(GeneKind.CATEGORICAL, default="9:16")


class GeneSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    group: GeneGroup
    kind: GeneKind


def _gene_specs() -> dict[str, GeneSpec]:
    specs = {}
    for name, field in Genes.model_fields.items():
        extra = field.json_schema_extra
        assert isinstance(extra, dict), f"gene {name} is missing its group/kind"
        specs[name] = GeneSpec(
            name=name, group=GeneGroup(str(extra["group"])), kind=GeneKind(str(extra["kind"]))
        )
    return specs


GENE_SPECS: dict[str, GeneSpec] = _gene_specs()


class Genome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = GENOME_SCHEMA_VERSION
    genes: Genes
    creative_spec: str = Field(min_length=1)


class UnsupportedGenomeVersion(Exception):
    pass


# Parsers by schema version. When the schema changes, add the new model here
# and an upgrade step; stored genomes are never rewritten.
_GENES_BY_VERSION: dict[int, type[Genes]] = {1: Genes}


def parse_genes(schema_version: int, data: dict[str, Any]) -> Genes:
    """Parse genes as stored (JSON) according to the version they were written with."""
    model = _GENES_BY_VERSION.get(schema_version)
    if model is None:
        raise UnsupportedGenomeVersion(f"no parser for genome schema version {schema_version}")
    return model.model_validate(data)


def genome_fingerprint(genome: Genome) -> str:
    """Stable hash of a genome's content: equal genomes, equal fingerprints."""
    canonical = json.dumps(genome.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class GeneDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    gene: str
    group: GeneGroup
    old: Any
    new: Any


def diff_genes(old: Genes, new: Genes) -> list[GeneDiff]:
    """Field-by-field comparison, in schema order."""
    return [
        GeneDiff(gene=name, group=spec.group, old=getattr(old, name), new=getattr(new, name))
        for name, spec in GENE_SPECS.items()
        if getattr(old, name) != getattr(new, name)
    ]
