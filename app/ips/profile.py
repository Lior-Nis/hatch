"""Structured specification of an IP: the world a brand's videos live in."""

from pydantic import BaseModel, ConfigDict, Field


class CharacterProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    role: str
    description: str
    """Personality and behaviour."""
    appearance: str
    """Exact visual description, repeated in every prompt for consistency."""


class IPProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    age_range: tuple[int, int]
    premise: str
    tone: str
    world_rules: tuple[str, ...] = Field(min_length=1)
    visual_identity: str
    safety_constraints: tuple[str, ...] = Field(min_length=1)
    differentiation: str
    """What makes this IP unlike the others in the portfolio."""
    characters: tuple[CharacterProfile, ...] = Field(min_length=1)
    suggested_handles: tuple[str, ...] = ()
    """Account-name ideas for the human who creates the social accounts."""
