from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin.web import create_app, get_asset_store, get_reviewer, get_session
from app.experiments.models import Experiment
from app.experiments.states import VideoStatus
from app.quality.models import HumanReview, ReviewDecision
from app.quality.ports import QAOutcome
from app.quality.runner import run_quality_gates
from integrations.fake.quality import FakeQAGate
from integrations.object_storage.local import LocalAssetStore
from tests.factories import (
    final_asset,
    make_generated_experiment,
    make_reviewable_experiment,
)


@pytest.fixture
def store(tmp_path: Path) -> LocalAssetStore:
    return LocalAssetStore(tmp_path / "assets")


@pytest.fixture
def client(session: Session, store: LocalAssetStore) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_asset_store] = lambda: store
    app.dependency_overrides[get_reviewer] = lambda: "lior"
    with TestClient(app, follow_redirects=False) as client:
        yield client


@pytest.fixture
def experiment(session: Session, tmp_path: Path, store: LocalAssetStore) -> Experiment:
    return make_reviewable_experiment(session, tmp_path, store=store)


def test_queue_lists_the_generated_short_awaiting_review(
    client: TestClient, experiment: Experiment
) -> None:
    response = client.get("/review")

    assert response.status_code == 200
    assert f"/review/{experiment.id}" in response.text
    assert "Nibbin Hollow" in response.text


def test_review_page_shows_video_hypothesis_genome_and_cost(
    client: TestClient, experiment: Experiment
) -> None:
    response = client.get(f"/review/{experiment.id}")

    assert response.status_code == 200
    asset = final_asset(experiment)
    assert f'src="/assets/{asset.id}/content"' in response.text
    assert "Opening on an unexplained gentle glow" in response.text
    assert "visual_question" in response.text
    assert "$0.40" in response.text


def test_asset_content_is_served_as_video(client: TestClient, experiment: Experiment) -> None:
    asset = final_asset(experiment)

    response = client.get(f"/assets/{asset.id}/content")

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert len(response.content) == asset.size_bytes


def test_approving_persists_the_decision_and_returns_to_the_queue(
    client: TestClient, session: Session, experiment: Experiment
) -> None:
    response = client.post(
        f"/review/{experiment.id}", data={"decision": "approve", "reason": "Lovely."}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/review"
    review = session.scalars(select(HumanReview)).one()
    assert (review.decision, review.reason, review.reviewer) == (
        ReviewDecision.APPROVE,
        "Lovely.",
        "lior",
    )
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.READY


def test_rejecting_without_a_reason_shows_an_error_and_changes_nothing(
    client: TestClient, session: Session, experiment: Experiment
) -> None:
    response = client.post(f"/review/{experiment.id}", data={"decision": "reject", "reason": ""})

    assert response.status_code == 422
    assert "reason is required" in response.text
    assert session.scalars(select(HumanReview)).all() == []
    assert session.get_one(Experiment, experiment.id).video_status is VideoStatus.APPROVAL_PENDING


def test_decided_reviews_are_shown_as_an_audit_trail(
    client: TestClient, experiment: Experiment
) -> None:
    client.post(f"/review/{experiment.id}", data={"decision": "reject", "reason": "Too dark."})

    queue = client.get("/review")
    detail = client.get(f"/review/{experiment.id}")

    assert "Too dark." in queue.text
    assert "Too dark." in detail.text
    assert "lior" in detail.text
    assert 'name="decision"' not in detail.text


def test_unknown_experiment_returns_404(client: TestClient) -> None:
    response = client.get("/review/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


def test_stage_a_queue_is_worked_through_one_video_after_another(
    client: TestClient, session: Session, tmp_path: Path, store: LocalAssetStore
) -> None:
    first, second, third = (
        make_reviewable_experiment(session, tmp_path, store=store) for _ in range(3)
    )

    after_first = client.post(f"/review/{first.id}", data={"decision": "approve", "reason": ""})
    after_second = client.post(
        f"/review/{second.id}", data={"decision": "reject", "reason": "Flat ending."}
    )
    after_third = client.post(f"/review/{third.id}", data={"decision": "approve", "reason": ""})

    assert after_first.headers["location"] == f"/review/{second.id}"
    assert after_second.headers["location"] == f"/review/{third.id}"
    assert after_third.headers["location"] == "/review"
    statuses = [session.get_one(Experiment, e.id).video_status for e in (first, second, third)]
    assert statuses == [VideoStatus.READY, VideoStatus.HUMAN_REJECTED, VideoStatus.READY]
    queue = client.get("/review")
    assert "Awaiting a decision (0)" in queue.text
    assert "3 of the first 100" in queue.text


def test_flagging_from_the_page_keeps_the_video_in_the_queue_with_a_marker(
    client: TestClient, experiment: Experiment
) -> None:
    response = client.post(
        f"/review/{experiment.id}", data={"decision": "flag", "reason": "Second look at audio."}
    )

    assert response.status_code == 303
    queue = client.get("/review")
    assert "Awaiting a decision (1)" in queue.text
    assert "flagged" in queue.text
    detail = client.get(f"/review/{experiment.id}")
    assert "Second look at audio." in detail.text
    assert 'value="flag"' in detail.text


def test_review_page_highlights_qa_escalations_and_needs_a_reason_to_approve(
    client: TestClient, session: Session, tmp_path: Path, store: LocalAssetStore
) -> None:
    experiment = make_generated_experiment(session, tmp_path, store=store)
    gate = FakeQAGate(
        name="child_safety", mandatory=True, outcome=QAOutcome.ESCALATE,
        reasons=["inappropriate_fear (possible): distorted face at 0:03"],
    )  # fmt: skip
    run_quality_gates(session, experiment.id, gates=[gate], store=store)

    page = client.get(f"/review/{experiment.id}")
    refused = client.post(f"/review/{experiment.id}", data={"decision": "approve", "reason": ""})

    assert "Needs your judgement" in page.text
    assert "distorted face at 0:03" in page.text
    assert refused.status_code == 422
    assert "escalat" in refused.text
