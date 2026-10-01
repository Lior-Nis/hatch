"""Adapter translation tests: requests hit a stub transport, never the network."""

import json
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from app.llm.ports import LanguageModel, LLMError, LLMRefused, LLMRequest
from integrations.anthropic.language_model import AnthropicLanguageModel


class Idea(BaseModel):
    title: str
    seconds: int


REQUEST = LLMRequest(
    purpose="test", system="You invent titles.", prompt="One title.", max_output_tokens=2000
)


def message(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps({"title": "Fireflies", "seconds": 8})}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    body.update(overrides)
    return body


class Stub:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def model(stub: Stub) -> AnthropicLanguageModel:
    client = anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(stub)),
    )
    return AnthropicLanguageModel(client=client, model="claude-opus-5-5", effort="high")


def test_adapter_satisfies_the_language_model_contract() -> None:
    llm: LanguageModel = model(Stub(httpx2.Response(200, json=message())))
    assert (llm.provider, llm.model) == ("anthropic", "claude-opus-5-5")


def test_generate_returns_the_validated_object_and_real_cost() -> None:
    stub = Stub(httpx2.Response(200, json=message(), headers={"request-id": "req_1"}))

    result = model(stub).generate(REQUEST, Idea)

    assert result.parsed == Idea(title="Fireflies", seconds=8)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1000, 500)
    # Opus 5.5: $4 / MTok in, $20 / MTok out.
    assert result.cost_usd == Decimal("0.014")
    assert result.model == "claude-opus-5-5"


def test_request_carries_system_prompt_schema_effort_and_fallbacks() -> None:
    stub = Stub(httpx2.Response(200, json=message()))

    model(stub).generate(REQUEST, Idea)

    body = json.loads(stub.requests[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 2000
    assert body["system"] == "You invent titles."
    assert body["messages"] == [{"role": "user", "content": "One title."}]
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert set(body["output_config"]["format"]["schema"]["properties"]) == {"title", "seconds"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in stub.requests[0].headers["anthropic-beta"]
    assert "thinking" not in body  # always on for this model; depth is set by effort


def test_estimate_is_an_upper_bound_using_the_output_token_cap() -> None:
    llm = model(Stub(httpx2.Response(200, json=message())))

    estimate = llm.estimate_cost(REQUEST)

    assert estimate >= Decimal("0.04")  # 2000 output tokens at $20 / MTok
    assert estimate < Decimal("0.05")


def test_refusal_raises_instead_of_returning_content() -> None:
    stub = Stub(
        httpx2.Response(
            200,
            json=message(
                content=[],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": None, "explanation": "declined"},
            ),
        )
    )

    with pytest.raises(LLMRefused):
        model(stub).generate(REQUEST, Idea)


def test_truncated_output_is_an_error() -> None:
    stub = Stub(
        httpx2.Response(
            200,
            json=message(
                content=[{"type": "text", "text": '{"title": "Fi'}], stop_reason="max_tokens"
            ),
        )
    )

    with pytest.raises(LLMError, match="max_tokens"):
        model(stub).generate(REQUEST, Idea)


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (529, True), (400, False), (401, False)]
)
def test_api_errors_are_classified_as_retryable_or_not(status: int, retryable: bool) -> None:
    stub = Stub(
        httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "nope"}})
    )

    with pytest.raises(LLMError) as error:
        model(stub).generate(REQUEST, Idea)

    assert error.value.retryable is retryable
    assert "test-key" not in str(error.value)


def test_an_unpriced_model_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="no price"):
        AnthropicLanguageModel(client=anthropic.Anthropic(api_key="k"), model="claude-unknown-9")
