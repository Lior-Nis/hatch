from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin.web import create_app, get_asset_store, get_reviewer, get_session
from app.budgets.models import BudgetLedgerEntry, LedgerStatus
from app.evolution.cycle import evolve_ip
from app.evolution.policy import EvolutionPolicy
from app.experiments.models import Experiment
from app.knowledge.synthesis import synthesize_ip_knowledge
from app.scheduling.queue import claim_next, enqueue, fail
from integrations.object_storage.local import LocalAssetStore
from tests.evolution.helpers import evaluated
from tests.factories import make_reviewable_experiment

POLICY = EvolutionPolicy()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(session: Session, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_asset_store] = lambda: LocalAssetStore(tmp_path / "assets")
    app.dependency_overrides[get_reviewer] = lambda: "lior"
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def world(session: Session, tmp_path: Path) -> dict[str, Experiment]:
    """A small portfolio: a replicated winner, a weak video, one awaiting
    review, a second IP with nothing yet, some spend and a failed job."""
    winner = evaluated(session, 0.9)
    evolve_ip(session, winner.ip, POLICY, pipeline_target=0, now=NOW)
    descendants = [evaluated(session, 0.7, parent=winner) for _ in range(3)]
    weak = evaluated(session, 0.3)
    evolve_ip(session, winner.ip, POLICY, pipeline_target=0, now=NOW)
    waiting = make_reviewable_experiment(session, tmp_path)
    evaluated(session, None, ip_slug="sock-planet")
    synthesize_ip_knowledge(session, winner.ip, POLICY)
    session.add(
        BudgetLedgerEntry(
            provider="higgsfield",
            model="alibaba/wan-3.0/text-to-video",
            operation="text_to_video",
            experiment_id=winner.id,
            estimated_cost_usd=Decimal("0.40"),
            actual_cost_usd=Decimal("0.40"),
            status=LedgerStatus.SETTLED,
        )
    )
    enqueue(session, "publish_video", payload={}, experiment_id=weak.id, max_attempts=1)
    job = claim_next(session, worker_id="w1")
    assert job is not None
    fail(session, job, error="Buffer refused the tiktok post: media not fetchable")
    session.flush()
    return {"winner": winner, "weak": weak, "waiting": waiting, "descendant": descendants[0]}


def test_portfolio_shows_what_is_winning_waiting_and_costing(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    page = client.get("/").text

    assert "Nibbin Hollow" in page and "Sock-Planet" in page
    assert "validated" in page  # the IP with a replicated winner
    assert "1 awaiting review" in page
    assert "1 failed" in page  # jobs needing attention
    assert "$15.00" in page and "$500.00" in page  # the ceilings spend is measured against
    assert 'href="/ips/nibbin-hollow"' in page


def test_ip_page_explains_status_fitness_experiments_and_knowledge(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    page = client.get("/ips/nibbin-hollow").text

    assert "validated" in page
    assert "hypothesis lineage(s) reproduced under replication" in page  # why it was promoted
    assert f'href="/experiments/{world["winner"].id}"' in page
    assert "supported" in page  # the winner's conclusion
    assert "0.90" in page and "0.30" in page  # experiment fitness, best and worst
    assert "SUPPORTED under replication" in page  # derived knowledge
    assert "youtube_shorts" in page  # per-platform IP fitness, not pooled


def test_experiment_page_shows_lineage_timeline_fitness_and_decisions(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    winner, descendant = world["winner"], world["descendant"]

    page = client.get(f"/experiments/{winner.id}").text
    child_page = client.get(f"/experiments/{descendant.id}").text

    assert winner.hypothesis.statement in page
    assert "request_replication" in page and "conclude_experiment" in page
    assert "One strong video is not proof" in page  # the recorded reason
    assert f'href="/experiments/{descendant.id}"' in page  # children
    assert "experiment_created" in page  # the timeline
    assert f'href="/experiments/{winner.id}"' in child_page  # parent
    assert "replication" in child_page


def test_queue_page_shows_failed_jobs_with_their_errors(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    page = client.get("/queue").text

    assert "publish_video" in page
    assert "media not fetchable" in page
    assert f'href="/experiments/{world["weak"].id}"' in page


def test_costs_page_shows_spend_and_budget_headroom(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    page = client.get("/costs").text

    assert "higgsfield" in page
    assert "nibbin-hollow" in page
    assert "Remaining today" in page
    assert "$0.40" in page or "$0.80" in page


def test_decisions_page_lists_reasons_and_can_filter_by_ip(
    client: TestClient, world: dict[str, Experiment]
) -> None:
    everything = client.get("/decisions").text
    filtered = client.get("/decisions?ip=sock-planet").text

    assert "promote_ip" in everything and "conclude_experiment" in everything
    assert "replication descendants reached" in everything
    assert "conclude_experiment" not in filtered


def test_unknown_ip_or_experiment_is_a_404(client: TestClient) -> None:
    assert client.get("/ips/nope").status_code == 404
    assert client.get("/experiments/00000000-0000-0000-0000-000000000000").status_code == 404


def test_every_page_links_to_the_others(client: TestClient, world: dict[str, Experiment]) -> None:
    page = client.get("/").text

    for path in ("/review", "/queue", "/costs", "/decisions"):
        assert f'href="{path}"' in page
