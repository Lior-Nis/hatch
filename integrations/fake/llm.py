"""Fake language model: returns scripted objects, free of charge by default."""

from collections.abc import Callable, Sequence
from decimal import Decimal

from pydantic import BaseModel

from app.llm.ports import LLMError, LLMRequest, LLMResult, LLMUsage

Scripted = BaseModel | Exception | Callable[[LLMRequest], BaseModel]


class FakeLanguageModel:
    provider = "fake"
    model = "fake-llm-1"

    def __init__(
        self,
        responses: Sequence[Scripted],
        *,
        cost_usd: Decimal = Decimal("0"),
        estimate_usd: Decimal | None = None,
    ) -> None:
        self._responses = list(responses)
        self._cost = cost_usd
        self._estimate = estimate_usd if estimate_usd is not None else cost_usd
        self.requests: list[LLMRequest] = []

    def estimate_cost(self, request: LLMRequest) -> Decimal:
        return self._estimate

    def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        self.requests.append(request)
        if not self._responses:
            raise LLMError("fake language model has no scripted response left")
        scripted = self._responses.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        value = scripted if isinstance(scripted, BaseModel) else scripted(request)
        return LLMResult[T](
            parsed=schema.model_validate(value.model_dump()),
            provider=self.provider,
            model=self.model,
            usage=LLMUsage(
                input_tokens=len(request.system + request.prompt) // 4 + 1,
                output_tokens=len(value.model_dump_json()) // 4 + 1,
            ),
            cost_usd=self._cost,
        )
