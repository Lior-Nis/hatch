"""Language model port.

Creative agents and QA gates ask a language model for a *structured* answer:
they pass a pydantic schema and get a validated instance back. Prompts are
built by Hatch (they are part of the decision record); the adapter only
transports them. No vendor types cross this boundary.
"""

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class LLMRefused(LLMError):
    """The model declined the request (safety policy)."""


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: str = Field(pattern=r"^[a-z0-9_]+$")
    """What the call is for (``hypothesis_mutation``, ``qa_child_safety``, ...).
    Recorded on the call and in the ledger operation."""
    system: str
    prompt: str
    max_output_tokens: int = Field(default=8000, gt=0)


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int
    output_tokens: int


class LLMResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: T
    provider: str
    model: str
    """The model that actually answered (may differ from the one requested if
    the provider fell back)."""
    usage: LLMUsage
    cost_usd: Decimal
    request_id: str | None = None


class LanguageModel(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def estimate_cost(self, request: LLMRequest) -> Decimal:
        """Upper bound in USD for one call, used for the budget reservation."""
        ...

    def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        """Return an instance of ``schema``. Raises ``LLMError``."""
        ...
