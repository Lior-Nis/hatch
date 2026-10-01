"""Quality gate port.

A gate inspects one candidate and returns a structured verdict. Gates marked
``mandatory`` block publication on FAIL or ESCALATE; nothing downstream
(fitness, predicted engagement, agent confidence) can override that.
"""

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class QAOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ESCALATE = "escalate"
    """Uncertain: requires a human decision before the candidate can proceed."""


class QACandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    experiment_id: str
    media_path: Path
    genes: dict[str, JsonValue]
    creative_spec: str
    hypothesis: str | None = None


class QAVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate: str
    gate_version: str
    outcome: QAOutcome
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    details: dict[str, JsonValue] = Field(default_factory=dict)


class QAGate(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def mandatory(self) -> bool: ...

    def evaluate(self, candidate: QACandidate) -> QAVerdict: ...
