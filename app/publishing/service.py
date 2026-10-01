"""Publishing: one approved canonical video → one package per platform →
scheduled through the ``Publisher`` port → status tracked by Hatch.

Hatch is the source of truth. Each ``Publication`` row carries Hatch's own id,
the schedule intent, the package that was sent, and the external ids that came
back. The publisher is asked at most once per publication: a publication that
already has an external id is never submitted again.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative.genome import parse_genes
from app.db import utcnow
from app.experiments.models import Experiment
from app.experiments.states import ExperimentStatus, VideoStatus
from app.ips.models import IP
from app.platforms import Platform
from app.production.models import Asset, AssetKind
from app.publishing.models import (
    AccountStatus,
    PlatformAccount,
    Publication,
    PublicationRecordStatus,
)
from app.publishing.packaging import PlatformPackage, build_packages, validate_package
from app.publishing.ports import (
    PublicationState,
    Publisher,
    PublishRequest,
    PublishTargetError,
)
from app.quality.models import HumanReview, QAResult, ReviewDecision
from app.quality.ports import QAOutcome
from app.storage import AssetStore

logger = logging.getLogger(__name__)

_TERMINAL = (
    PublicationRecordStatus.PUBLISHED,
    PublicationRecordStatus.FAILED,
    PublicationRecordStatus.CANCELLED,
)


class PublishNotAllowed(Exception):
    """The video must not be published (not approved, failed QA, no accounts)."""


def map_account(
    session: Session,
    ip: IP,
    platform: Platform,
    *,
    external_account_id: str,
    publisher_profile_id: str | None = None,
    handle: str | None = None,
) -> PlatformAccount:
    """Record that this human-owned account is the IP's presence on a
    platform. One account per IP per platform (enforced by the database)."""
    account = PlatformAccount(
        ip=ip,
        platform=platform,
        external_account_id=external_account_id,
        publisher_profile_id=publisher_profile_id,
        handle=handle,
    )
    session.add(account)
    session.flush()
    return account


def assert_publishable(session: Session, experiment: Experiment) -> Asset:
    """The safety boundary in front of publishing. Checked from the evidence,
    not from the status field alone: the current final asset must have passed
    automated QA with no mandatory failure and carry a human approval.
    Returns the asset to publish."""
    if experiment.video_status is not VideoStatus.READY:
        raise PublishNotAllowed(
            f"experiment {experiment.id} is not ready to publish "
            f"(video is {experiment.video_status.value})"
        )
    asset = session.scalars(
        select(Asset)
        .where(Asset.experiment_id == experiment.id, Asset.kind == AssetKind.FINAL_VIDEO)
        .order_by(Asset.created_at.desc())
    ).first()
    if asset is None:
        raise PublishNotAllowed(f"experiment {experiment.id} has no final video")
    results = session.scalars(select(QAResult).where(QAResult.asset_id == asset.id)).all()
    if not results:
        raise PublishNotAllowed(f"asset {asset.id} has not been through automated QA")
    failed = [r.gate for r in results if r.mandatory and r.outcome is QAOutcome.FAIL]
    if failed:
        raise PublishNotAllowed(
            f"asset {asset.id} failed mandatory QA ({', '.join(failed)}); it can never be published"
        )
    approved = session.scalars(
        select(HumanReview).where(
            HumanReview.asset_id == asset.id, HumanReview.decision == ReviewDecision.APPROVE
        )
    ).first()
    if approved is None:
        raise PublishNotAllowed(f"asset {asset.id} has no human approval on record")
    return asset


def _existing(session: Session, experiment_id: uuid.UUID) -> list[Publication]:
    return list(
        session.scalars(
            select(Publication)
            .where(Publication.experiment_id == experiment_id)
            .order_by(Publication.created_at)
        )
    )


def plan_publications(
    session: Session, experiment_id: uuid.UUID, *, scheduled_at: datetime | None
) -> list[Publication]:
    """Create one PLANNED publication per platform for an approved video.
    Idempotent: an experiment that is already planned returns its plan."""
    existing = _existing(session, experiment_id)
    if existing:
        return existing
    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    asset = assert_publishable(session, experiment)

    accounts = {
        account.platform: account
        for account in session.scalars(
            select(PlatformAccount).where(
                PlatformAccount.ip_id == experiment.ip_id,
                PlatformAccount.status == AccountStatus.ACTIVE,
            )
        )
    }
    missing = [platform.value for platform in Platform if platform not in accounts]
    if missing:
        raise PublishNotAllowed(
            f"IP {experiment.ip.slug} has no active account mapped for: {', '.join(missing)}"
        )

    genes = parse_genes(experiment.genome.schema_version, experiment.genome.genes)
    packages = build_packages(
        ip_name=experiment.ip.name, category=experiment.ip.category, genes=genes
    )
    planned = []
    for platform in Platform:
        validate_package(packages[platform])
        publication = Publication(
            experiment=experiment,
            asset=asset,
            platform=platform,
            platform_account=accounts[platform],
            package=packages[platform].model_dump(mode="json"),
            scheduled_at=scheduled_at,
        )
        session.add(publication)
        planned.append(publication)
    session.commit()
    return planned


def submit_publications(
    session: Session, experiment_id: uuid.UUID, *, publisher: Publisher, store: AssetStore
) -> list[Publication]:
    """Hand every PLANNED publication to the publisher. A publication that
    already has an external id is skipped, so repeating this is safe."""
    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    publications = _existing(session, experiment_id)
    for publication in publications:
        account = publication.platform_account
        if account is None or account.ip_id != experiment.ip_id:
            raise PublishTargetError(
                f"publication {publication.id} targets an account that is not mapped to "
                f"IP {experiment.ip.slug}"
            )
    if any(p.status is PublicationRecordStatus.PLANNED for p in publications):
        assert_publishable(session, experiment)

    for publication in publications:
        if publication.status is not PublicationRecordStatus.PLANNED or publication.external_id:
            continue
        package = PlatformPackage.model_validate(publication.package)
        account = publication.platform_account
        assert account is not None
        request = PublishRequest(
            publication_id=str(publication.id),
            platform=publication.platform,
            platform_account_id=account.publisher_profile_id or account.external_account_id,
            media_url=store.public_url(publication.asset.storage_uri),
            title=package.title,
            text=package.text(),
            thumbnail_offset_ms=package.thumbnail_offset_ms,
            made_for_kids=package.made_for_kids,
            ai_generated=package.ai_generated,
            scheduled_at=publication.scheduled_at,
        )
        try:
            receipt = (
                publisher.schedule(request) if request.scheduled_at else publisher.publish(request)
            )
        except PublishTargetError as exc:
            publication.status = PublicationRecordStatus.FAILED
            publication.error = str(exc)
            logger.warning(
                "publication_failed",
                extra={"experiment_id": str(experiment.id), "platform": publication.platform.value},
            )
        else:
            publication.publisher = receipt.provider
            publication.external_id = receipt.external_id
            publication.status = PublicationRecordStatus.SCHEDULED
        # Commit per publication: an external post must never exist without
        # its id being stored.
        session.commit()

    _update_video_status(experiment, publications)
    session.commit()
    return publications


def refresh_publications(
    session: Session, experiment_id: uuid.UUID, *, publisher: Publisher
) -> list[Publication]:
    """Ask the publisher what happened to each scheduled post and record it."""
    experiment = session.get_one(Experiment, experiment_id, with_for_update=True)
    publications = _existing(session, experiment_id)
    for publication in publications:
        if publication.status is not PublicationRecordStatus.SCHEDULED:
            continue
        assert publication.external_id is not None
        status = publisher.get_status(publication.external_id)
        if status.state is PublicationState.PUBLISHED:
            publication.status = PublicationRecordStatus.PUBLISHED
            publication.platform_post_id = status.platform_post_id
            publication.permalink = status.permalink
            publication.published_at = status.published_at or utcnow()
        elif status.state is PublicationState.FAILED:
            publication.status = PublicationRecordStatus.FAILED
            publication.error = status.error
        elif status.state is PublicationState.CANCELLED:
            publication.status = PublicationRecordStatus.CANCELLED
    _update_video_status(experiment, publications)
    session.commit()
    return publications


def _update_video_status(experiment: Experiment, publications: list[Publication]) -> None:
    """Roll the per-platform states up to the video. A video is PUBLISHED once
    every platform has reported and at least one succeeded."""
    statuses = [publication.status for publication in publications]
    if not statuses or PublicationRecordStatus.PLANNED in statuses:
        return
    if experiment.video_status is VideoStatus.READY:
        experiment.video_status = VideoStatus.SCHEDULED
    if experiment.video_status is not VideoStatus.SCHEDULED:
        return
    if all(status in _TERMINAL for status in statuses):
        if PublicationRecordStatus.PUBLISHED in statuses:
            experiment.video_status = VideoStatus.PUBLISHED
            if experiment.status is ExperimentStatus.RUNNING:
                experiment.status = ExperimentStatus.OBSERVING
        else:
            experiment.video_status = VideoStatus.PUBLISH_FAILED
