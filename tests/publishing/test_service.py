from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.ips.models import IP
from app.platforms import Platform
from app.publishing.models import (
    AccountStatus,
    PlatformAccount,
    Publication,
    PublicationRecordStatus,
)
from app.publishing.packaging import PlatformPackage, validate_package
from app.publishing.ports import PublishTargetError
from app.publishing.service import (
    PublishNotAllowed,
    map_account,
    plan_publications,
    refresh_publications,
    submit_publications,
)
from app.quality.models import ReviewDecision
from app.quality.review import submit_review
from integrations.fake.publisher import FakePublisher
from integrations.object_storage.local import LocalAssetStore
from tests.factories import (
    final_asset,
    force_video_status,
    make_experiment,
    make_reviewable_experiment,
)

WHEN = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
CHANNELS = {
    Platform.YOUTUBE_SHORTS: "buf-yt",
    Platform.TIKTOK: "buf-tt",
    Platform.INSTAGRAM_REELS: "buf-ig",
    Platform.FACEBOOK_REELS: "buf-fb",
}


@pytest.fixture
def store(tmp_path: Path) -> LocalAssetStore:
    return LocalAssetStore(tmp_path / "assets", public_base_url="https://media.example.com")


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher(accounts=set(CHANNELS.values()))


def map_all(session: Session, ip: IP) -> None:
    for platform, channel in CHANNELS.items():
        map_account(
            session,
            ip,
            platform,
            external_account_id=f"native-{platform.value}",
            publisher_profile_id=channel,
            handle="@nibbinhollow",
        )


@pytest.fixture
def approved(session: Session, tmp_path: Path, store: LocalAssetStore) -> Experiment:
    experiment = make_reviewable_experiment(session, tmp_path, store=store)
    submit_review(
        session, experiment.id, decision=ReviewDecision.APPROVE, reason="Good.", reviewer="lior"
    )
    map_all(session, experiment.ip)
    session.expire_all()
    return session.get_one(Experiment, experiment.id)


def publications(session: Session) -> list[Publication]:
    return list(session.scalars(select(Publication).order_by(Publication.created_at)))


# --- account mapping ---------------------------------------------------------


def test_an_ip_has_one_account_per_platform(session: Session) -> None:
    ip = make_experiment(session).ip
    map_all(session, ip)

    with pytest.raises(IntegrityError):
        map_account(
            session, ip, Platform.TIKTOK, external_account_id="other", publisher_profile_id="x"
        )


# --- planning ----------------------------------------------------------------


def test_one_canonical_video_is_packaged_for_all_four_platforms(
    session: Session, approved: Experiment
) -> None:
    planned = plan_publications(session, approved.id, scheduled_at=WHEN)

    assert {p.platform for p in planned} == set(Platform)
    assert {p.asset_id for p in planned} == {final_asset(approved).id}  # same video everywhere
    assert all(p.status is PublicationRecordStatus.PLANNED for p in planned)
    assert all(p.scheduled_at == WHEN for p in planned)
    for publication in planned:
        package = PlatformPackage.model_validate(publication.package)
        validate_package(package)
        assert package.platform is publication.platform
        assert publication.platform_account is not None
        assert publication.platform_account.ip_id == approved.ip_id


def test_planning_twice_does_not_duplicate_publications(
    session: Session, approved: Experiment
) -> None:
    first = plan_publications(session, approved.id, scheduled_at=WHEN)
    second = plan_publications(session, approved.id, scheduled_at=WHEN)

    assert [p.id for p in first] == [p.id for p in second]
    assert len(publications(session)) == 4


def test_a_video_without_human_approval_cannot_be_planned(
    session: Session, tmp_path: Path, store: LocalAssetStore
) -> None:
    experiment = make_reviewable_experiment(session, tmp_path, store=store)
    map_all(session, experiment.ip)

    with pytest.raises(PublishNotAllowed, match="approval_pending"):
        plan_publications(session, experiment.id, scheduled_at=WHEN)


def test_the_ready_status_alone_is_not_trusted(
    session: Session, tmp_path: Path, store: LocalAssetStore
) -> None:
    """Even if the status were wrong, a video with no recorded human approval
    of its current asset must not be published."""
    experiment = make_reviewable_experiment(session, tmp_path, store=store)
    map_all(session, experiment.ip)
    force_video_status(session, experiment, VideoStatus.READY)

    with pytest.raises(PublishNotAllowed, match="human approval"):
        plan_publications(session, experiment.id, scheduled_at=WHEN)


def test_missing_platform_accounts_are_named_and_nothing_is_planned(
    session: Session, approved: Experiment
) -> None:
    session.execute(
        update(PlatformAccount)
        .where(PlatformAccount.platform == Platform.TIKTOK)
        .values(status=AccountStatus.DISCONNECTED)
    )

    with pytest.raises(PublishNotAllowed, match="tiktok"):
        plan_publications(session, approved.id, scheduled_at=WHEN)

    assert publications(session) == []


# --- submitting ----------------------------------------------------------------


def test_submission_schedules_every_package_and_persists_external_ids(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN)

    submit_publications(session, approved.id, publisher=publisher, store=store)

    stored = publications(session)
    assert all(p.status is PublicationRecordStatus.SCHEDULED for p in stored)
    assert all(p.external_id and p.publisher == "fake" for p in stored)
    assert len({p.external_id for p in stored}) == 4
    assert session.get_one(Experiment, approved.id).video_status is VideoStatus.SCHEDULED
    for publication in stored:
        request = publisher.requests[publication.external_id or ""]
        assert request.platform_account_id == CHANNELS[publication.platform]
        assert request.media_url.startswith("https://media.example.com/experiments/")
        assert request.scheduled_at == WHEN
        assert request.text == PlatformPackage.model_validate(publication.package).text()
    youtube = next(r for r in publisher.requests.values() if r.platform is Platform.YOUTUBE_SHORTS)
    assert youtube.made_for_kids is True and youtube.title
    assert len({r.media_url for r in publisher.requests.values()}) == 1  # one canonical file


def test_submitting_again_does_not_create_duplicate_posts(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN)
    submit_publications(session, approved.id, publisher=publisher, store=store)

    submit_publications(session, approved.id, publisher=publisher, store=store)

    assert publisher.post_count == 4
    assert len(publications(session)) == 4


def test_publishing_cannot_target_an_account_of_another_ip(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    other_ip = make_experiment(session, ip_slug="zed-lab").ip
    foreign = map_account(
        session,
        other_ip,
        Platform.TIKTOK,
        external_account_id="zed",
        publisher_profile_id="buf-zed",
    )
    plan_publications(session, approved.id, scheduled_at=WHEN)
    session.execute(
        update(Publication)
        .where(Publication.platform == Platform.TIKTOK)
        .values(platform_account_id=foreign.id)
    )
    session.expire_all()

    with pytest.raises(PublishTargetError, match="not mapped"):
        submit_publications(session, approved.id, publisher=publisher, store=store)

    assert publisher.post_count == 0


def test_a_platform_that_refuses_the_post_fails_alone(
    session: Session, approved: Experiment, store: LocalAssetStore
) -> None:
    publisher = FakePublisher(accounts=set(CHANNELS.values()) - {"buf-tt"})
    plan_publications(session, approved.id, scheduled_at=WHEN)

    submit_publications(session, approved.id, publisher=publisher, store=store)

    by_platform = {p.platform: p for p in publications(session)}
    assert by_platform[Platform.TIKTOK].status is PublicationRecordStatus.FAILED
    assert "not connected" in (by_platform[Platform.TIKTOK].error or "")
    assert by_platform[Platform.YOUTUBE_SHORTS].status is PublicationRecordStatus.SCHEDULED
    assert session.get_one(Experiment, approved.id).video_status is VideoStatus.SCHEDULED


def test_when_every_platform_refuses_the_video_is_publish_failed(
    session: Session, approved: Experiment, store: LocalAssetStore
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN)

    submit_publications(session, approved.id, publisher=FakePublisher(accounts=set()), store=store)

    assert session.get_one(Experiment, approved.id).video_status is VideoStatus.PUBLISH_FAILED


# --- status refresh --------------------------------------------------------------


def test_refresh_records_platform_post_ids_and_marks_the_video_published(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN)
    submit_publications(session, approved.id, publisher=publisher, store=store)
    for publication in publications(session):
        publisher.deliver(publication.external_id or "")

    refresh_publications(session, approved.id, publisher=publisher)

    stored = publications(session)
    assert all(p.status is PublicationRecordStatus.PUBLISHED for p in stored)
    assert all(p.platform_post_id and p.permalink and p.published_at for p in stored)
    experiment = session.get_one(Experiment, approved.id)
    assert experiment.video_status is VideoStatus.PUBLISHED
    assert experiment.status is ExperimentStatus.OBSERVING


def test_video_stays_scheduled_until_every_platform_has_reported(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN + timedelta(hours=1))
    submit_publications(session, approved.id, publisher=publisher, store=store)
    first = publications(session)[0]
    publisher.deliver(first.external_id or "")

    refresh_publications(session, approved.id, publisher=publisher)

    assert session.get_one(Publication, first.id).status is PublicationRecordStatus.PUBLISHED
    assert session.get_one(Experiment, approved.id).video_status is VideoStatus.SCHEDULED


def test_a_post_that_failed_on_the_platform_is_recorded_with_its_error(
    session: Session, approved: Experiment, store: LocalAssetStore, publisher: FakePublisher
) -> None:
    plan_publications(session, approved.id, scheduled_at=WHEN)
    submit_publications(session, approved.id, publisher=publisher, store=store)
    stored = publications(session)
    publisher.fail(stored[0].external_id or "", "video rejected by platform")
    for publication in stored[1:]:
        publisher.deliver(publication.external_id or "")

    refresh_publications(session, approved.id, publisher=publisher)

    failed = session.get_one(Publication, stored[0].id)
    assert failed.status is PublicationRecordStatus.FAILED
    assert failed.error == "video rejected by platform"
    assert session.get_one(Experiment, approved.id).video_status is VideoStatus.PUBLISHED
