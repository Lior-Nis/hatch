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
from integrations.object_storage.local import LocalAssetStore
from tests.factories import make_reviewable_experiment


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
    asset = experiment.assets[0]
    assert f'src="/assets/{asset.id}/content"' in response.text
    assert "Opening on an unexplained gentle glow" in response.text
    assert "visual_question" in response.text
    assert "$0.40" in response.text


def test_asset_content_is_served_as_video(client: TestClient, experiment: Experiment) -> None:
    asset = experiment.assets[0]

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
