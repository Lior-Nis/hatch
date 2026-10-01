"""Budget-governed, recorded language-model calls.

Every call reserves its estimated cost first, is stored as a ``ModelCall``
(prompt, response, tokens, cost), and settles the ledger with the real cost.
"""

import uuid
from collections.abc import Callable, Iterable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.experiments.models import Experiment
from app.llm.models import ModelCall
from app.llm.ports import LanguageModel, LLMError, LLMRequest, LLMResult


def call_model[T: BaseModel](
    session: Session,
    llm: LanguageModel,
    governor: BudgetGovernor,
    request: LLMRequest,
    schema: type[T],
    *,
    experiment_id: uuid.UUID | None = None,
    recorded: list[uuid.UUID] | None = None,
) -> LLMResult[T]:
    """Raises ``BudgetExceeded`` (nothing called) or ``LLMError`` (call recorded).
    The id of the stored ``ModelCall`` is appended to ``recorded`` when given."""
    reservation = governor.reserve(
        session,
        provider=llm.provider,
        model=llm.model,
        operation=f"llm:{request.purpose}",
        estimated_cost_usd=llm.estimate_cost(request),
        experiment_id=experiment_id,
    )
    session.commit()  # the reservation must outlive a crash during the call
    call = ModelCall(
        purpose=request.purpose,
        provider=llm.provider,
        model=llm.model,
        system=request.system,
        prompt=request.prompt,
        ledger_entry=reservation,
        experiment_id=experiment_id,
    )
    try:
        result = llm.generate(request, schema)
    except LLMError as exc:
        call.error = str(exc)
        session.add(call)
        session.flush()
        if recorded is not None:
            recorded.append(call.id)
        # Whether a failed call was billed is unknown: keep counting the estimate.
        governor.settle(session, reservation, actual_cost_usd=None)
        session.commit()
        raise
    call.model = result.model
    call.response = result.parsed.model_dump(mode="json")
    call.input_tokens = result.usage.input_tokens
    call.output_tokens = result.usage.output_tokens
    call.cost_usd = result.cost_usd
    session.add(call)
    session.flush()
    if recorded is not None:
        recorded.append(call.id)
    governor.settle(session, reservation, actual_cost_usd=result.cost_usd)
    session.commit()
    return result


def attribute_to_experiment(
    session: Session, call_ids: Iterable[uuid.UUID], experiment: Experiment
) -> None:
    """Link calls made before a candidate existed (and their cost) to it."""
    for call in session.scalars(select(ModelCall).where(ModelCall.id.in_(list(call_ids)))):
        if call.experiment_id is not None:
            raise ValueError(f"model call {call.id} is already attributed to an experiment")
        call.experiment_id = experiment.id
        if call.ledger_entry is not None:
            call.ledger_entry.experiment_id = experiment.id
    session.flush()


def bound_caller[T: BaseModel](
    session: Session, llm: LanguageModel, governor: BudgetGovernor
) -> Callable[[LLMRequest, type[T], str | None], T]:
    """A plain ``(request, schema, experiment_id) → parsed`` function for code
    that should not know about sessions or budgets (e.g. QA gates)."""

    def call(request: LLMRequest, schema: type[T], experiment_id: str | None) -> T:
        return call_model(
            session,
            llm,
            governor,
            request,
            schema,
            experiment_id=uuid.UUID(experiment_id) if experiment_id else None,
        ).parsed

    return call
