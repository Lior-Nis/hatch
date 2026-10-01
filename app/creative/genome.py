"""Creative genome, schema version 1.

A genome is the structured, queryable part of a candidate: creative genes
(what the video is) plus production genes (how it is made). The free-form
creative specification travels alongside it. Unknown genes are rejected so a
typo can never silently become an unanalysable gene.
"""

from pydantic import BaseModel, ConfigDict, Field

GENOME_SCHEMA_VERSION = 1


class Genes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- creative genes ---
    primary_character: str
    supporting_characters: tuple[str, ...] = ()
    topic: str
    story_archetype: str
    educational_goal: str | None = None
    hook_type: str
    dominant_emotion: str
    visual_style: str
    camera_style: str
    duration_seconds: int = Field(gt=0)
    scene_count: int = Field(gt=0)
    pace: str
    narrator_style: str
    dialogue_density: str
    music_style: str
    ending_type: str
    cta_type: str

    # --- production genes ---
    video_model: str
    image_model: str | None = None
    prompt_strategy: str
    resolution: str
    aspect_ratio: str = "9:16"


class Genome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = GENOME_SCHEMA_VERSION
    genes: Genes
    creative_spec: str = Field(min_length=1)
