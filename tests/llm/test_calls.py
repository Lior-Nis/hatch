from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetExceeded, BudgetGovernor
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.llm.calls import attribute_to_experiment, call_model
from app.llm.models import ModelCall
from app.llm.ports import LanguageModel, LLMError, LLMRequest
from integrations.fake.llm import FakeLanguageModel
from tests.factories import LIMITS, make_experiment


class Idea(BaseModel):
    title: str


REQUEST = LLMRequest(purpose="test_idea", system="You invent titles.", prompt="One title please.")


def governor() -> BudgetGovernor:
    return BudgetGovernor(LIMITS)


def test_fake_model_satisfies_the_language_model_contract() -> None:
    model: LanguageModel = FakeLanguageModel([Idea(title="x")])
    assert (model.provider, model.model) == ("fake", "fake-llm-1")


def test_call_returns_the_parsed_object(session: Session) -> None:
    llm = FakeLanguageModel([Idea(title="Fireflies")])

    result = call_model(session, llm, governor(), REQUEST, Idea)

    assert result.parsed == Idea(title="Fireflies")
    assert llm.requests == [REQUEST]


def test_call_is_recorded_with_prompt_response_model_and_cost(session: Session) -> None:
    llm = FakeLanguageModel([Idea(title="Fireflies")], cost_usd=Decimal("0.03"))

    call_model(session, llm, governor(), REQUEST, Idea)

    call = session.scalars(select(ModelCall)).one()
    assert (call.purpose, call.provider, call.model) == ("test_idea", "fake", "fake-llm-1")
    assert (call.system, call.prompt) == (REQUEST.system, REQUEST.prompt)
    assert call.response == {"title": "Fireflies"}
    assert call.cost_usd == Decimal("0.03")
    assert call.input_tokens and call.output_tokens
    assert call.error is None


def test_call_is_charged_through_the_budget_ledger(session: Session) -> None:
    llm = FakeLanguageModel(
        [Idea(title="x")], cost_usd=Decimal("0.03"), estimate_usd=Decimal("0.10")
    )

    call_model(session, llm, governor(), REQUEST, Idea)

    entry = session.scalars(select(BudgetLedgerEntry)).one()
    assert (entry.provider, entry.model, entry.operation) == ("fake", "fake-llm-1", "llm:test_idea")
    assert entry.status is LedgerStatus.SETTLED
    assert entry.estimated_cost_usd == Decimal("0.10")
    assert entry.actual_cost_usd == Decimal("0.03")
    assert session.scalars(select(ModelCall)).one().ledger_entry_id == entry.id


def test_call_over_budget_is_blocked_before_the_model_is_called(session: Session) -> None:
    llm = FakeLanguageModel([Idea(title="x")], estimate_usd=Decimal("5.00"))

    with pytest.raises(BudgetExceeded):
        call_model(session, llm, governor(), REQUEST, Idea)

    assert llm.requests == []
    assert session.scalars(select(ModelCall)).all() == []


def test_failed_call_is_recorded_and_its_reservation_closed(session: Session) -> None:
    llm = FakeLanguageModel([LLMError("overloaded", retryable=True)])

    with pytest.raises(LLMError):
        call_model(session, llm, governor(), REQUEST, Idea)

    call = session.scalars(select(ModelCall)).one()
    assert call.error == "overloaded"
    assert call.response is None
    assert session.scalars(select(BudgetLedgerEntry)).one().status is LedgerStatus.SETTLED


def test_call_made_before_the_experiment_exists_can_be_attributed_once(session: Session) -> None:
    llm = FakeLanguageModel([Idea(title="x")], cost_usd=Decimal("0.03"))
    call_model(session, llm, governor(), REQUEST, Idea)
    call = session.scalars(select(ModelCall)).one()
    experiment, other = make_experiment(session), make_experiment(session)

    attribute_to_experiment(session, [call.id], experiment)

    session.expire_all()
    assert session.get_one(ModelCall, call.id).experiment_id == experiment.id
    assert session.scalars(select(BudgetLedgerEntry)).one().experiment_id == experiment.id
    assert governor().committed_spend_for_experiment(session, experiment.id) == Decimal("0.03")
    with pytest.raises(ValueError, match="already attributed"):
        attribute_to_experiment(session, [call.id], other)
