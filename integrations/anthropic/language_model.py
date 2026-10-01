"""Claude adapter for the ``LanguageModel`` port (official Anthropic SDK).

Structured output via ``messages.parse`` (the SDK validates the response
against the pydantic schema). Thinking is always on for current models and is
steered with ``effort``. Server-side refusal fallbacks are enabled, so a
policy decline is retried on a fallback model inside the same call; the cost is
priced by the model that actually answered.
"""

import math
from decimal import Decimal
from typing import Literal

import anthropic
from anthropic.types.beta import BetaOutputConfigParam
from pydantic import BaseModel, ValidationError

from app.llm.ports import LLMError, LLMRefused, LLMRequest, LLMResult, LLMUsage

DEFAULT_MODEL = "claude-opus-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# USD per million tokens (input, output). First-party API list prices.
_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}
_MILLION = Decimal("1000000")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _cost(model: str, input_tokens: int, output_tokens: int, default: str) -> Decimal:
    price_in, price_out = _PRICES.get(model) or _PRICES[default]
    return (price_in * input_tokens + price_out * output_tokens) / _MILLION


class AnthropicLanguageModel:
    provider = "anthropic"

    def __init__(
        self,
        *,
        client: anthropic.Anthropic,
        model: str = DEFAULT_MODEL,
        effort: Effort = "high",
    ) -> None:
        if model not in _PRICES:
            raise ValueError(f"no price is known for model {model!r}; cannot account for its cost")
        self._client = client
        self.model = model
        self._effort = effort

    def estimate_cost(self, request: LLMRequest) -> Decimal:
        # Conservative: ~3 characters per token in, and the full output cap out.
        input_tokens = math.ceil(len(request.system + request.prompt) / 3)
        return _cost(self.model, input_tokens, request.max_output_tokens, self.model)

    def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        try:
            response = self._client.beta.messages.parse(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
                output_format=schema,
                output_config=BetaOutputConfigParam(effort=self._effort),
                betas=[_FALLBACK_BETA],
                fallbacks="default",
            )
        except ValidationError as exc:
            # The SDK validates the reply itself; a reply cut off at max_tokens
            # surfaces here as invalid JSON.
            raise LLMError(
                "Claude returned invalid structured output (possibly truncated at max_tokens): "
                f"{exc.error_count()} validation error(s)"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Claude rate limited: {exc.message}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"Claude API error {exc.status_code}: {exc.message}",
                retryable=exc.status_code >= 500,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach Claude: {exc}", retryable=True) from exc

        if response.stop_reason == "refusal":
            details = response.stop_details
            raise LLMRefused(
                f"Claude declined the request ({getattr(details, 'category', None)}): "
                f"{getattr(details, 'explanation', None)}"
            )
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise LLMError(
                f"Claude returned no complete structured output "
                f"(stop_reason={response.stop_reason})"
            )
        usage = LLMUsage(
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
        )
        return LLMResult[T](
            parsed=response.parsed_output,
            provider=self.provider,
            model=response.model,
            usage=usage,
            cost_usd=_cost(response.model, usage.input_tokens, usage.output_tokens, self.model),
            request_id=response._request_id,
        )
