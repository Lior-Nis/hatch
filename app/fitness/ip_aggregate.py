"""IP-level fitness: how an IP is doing across its recent videos.

VideoFitness and IPFitness are different things. An IP score is built in two
steps so that incompatible audience signals are never pooled:

1. Per platform, take each recent video's most mature fitness (already
   relative to that platform account's own baseline) and compute a
   confidence-weighted *median*. A median means one flop — or one viral
   outlier — moves the IP very little.
2. Combine the platform medians with fixed platform weights, counting only
   platforms that have enough videos to say anything.

With too few videos everywhere the IP score is a neutral 0.5 and flagged as
insufficient evidence: nothing should be promoted or archived on it.
"""

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.ingestion import CHECKPOINTS
from app.experiments.models import Experiment
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.ips.models import IP
from app.platforms import Platform

logger = logging.getLogger(__name__)

IP_AGGREGATE_NAME = "ip_aggregate"
IP_AGGREGATE_VERSION = "1"

_MATURITY = {checkpoint: rank for rank, checkpoint in enumerate(CHECKPOINTS)}


class IPAggregationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: int = 20
    """How many of the IP's most recent videos are considered."""
    min_videos: int = 3
    """Videos a platform needs before its median counts."""
    platform_weights: dict[Platform, float] = {
        # YouTube Shorts is the direct children's surface; the others are
        # mostly parent/family discovery.
        Platform.YOUTUBE_SHORTS: 0.4,
        Platform.TIKTOK: 0.2,
        Platform.INSTAGRAM_REELS: 0.2,
        Platform.FACEBOOK_REELS: 0.2,
    }


def weighted_median(values: Sequence[tuple[float, float]]) -> float:
    """Median of ``(value, weight)`` pairs."""
    ordered = sorted(values)
    half = sum(weight for _, weight in ordered) / 2
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= half:
            return value
    return ordered[-1][0]


def aggregate_ip_fitness(
    session: Session, ip: IP, *, policy: IPAggregationPolicy | None = None
) -> FitnessSnapshot:
    """Compute and store the IP's current fitness. Every call appends a new
    snapshot: IP fitness is a history, not a single mutable number."""
    policy = policy or IPAggregationPolicy()
    recent = session.scalars(
        select(Experiment.id)
        .where(
            Experiment.ip_id == ip.id,
            Experiment.id.in_(
                select(FitnessSnapshot.experiment_id).where(
                    FitnessSnapshot.scope == FitnessScope.VIDEO, FitnessSnapshot.ip_id == ip.id
                )
            ),
        )
        .order_by(Experiment.created_at.desc())
        .limit(policy.window)
    ).all()
    rows = session.scalars(
        select(FitnessSnapshot).where(
            FitnessSnapshot.scope == FitnessScope.VIDEO,
            FitnessSnapshot.experiment_id.in_(recent),
        )
    ).all()

    # The most mature evaluation of each video on each platform.
    best: dict[tuple[Any, Platform], FitnessSnapshot] = {}
    for row in rows:
        if row.platform is None:
            continue
        key = (row.experiment_id, row.platform)
        rank = (_MATURITY.get(row.checkpoint or "", -1), row.created_at)
        current = best.get(key)
        if current is None or rank > (
            _MATURITY.get(current.checkpoint or "", -1),
            current.created_at,
        ):
            best[key] = row

    platforms: dict[str, dict[str, Any]] = {}
    components: dict[str, float] = {}
    for platform in Platform:
        entries = [row for (_, row_platform), row in best.items() if row_platform is platform]
        if not entries:
            continue
        pairs = [
            (row.score, max(float(row.inputs.get("confidence", 1.0)), 0.01)) for row in entries
        ]
        median = weighted_median(pairs)
        sufficient = len(entries) >= policy.min_videos
        platforms[platform.value] = {
            "score": median,
            "videos": len(entries),
            "sufficient": sufficient,
            "experiments": [
                {
                    "experiment_id": str(row.experiment_id),
                    "score": row.score,
                    "confidence": row.inputs.get("confidence", 1.0),
                    "checkpoint": row.checkpoint,
                    "fitness_snapshot_id": str(row.id),
                }
                for row in sorted(entries, key=lambda r: r.created_at)
            ],
        }
        if sufficient:
            components[platform.value] = median

    weights = {name: policy.platform_weights[Platform(name)] for name in components}
    if components:
        score = sum(components[name] * weights[name] for name in components) / sum(weights.values())
    else:
        score = 0.5
    snapshot = FitnessSnapshot(
        scope=FitnessScope.IP,
        ip=ip,
        evaluator=IP_AGGREGATE_NAME,
        evaluator_version=IP_AGGREGATE_VERSION,
        score=round(score, 6),
        components=components,
        inputs={
            "platforms": platforms,
            "platform_weights": {p.value: w for p, w in policy.platform_weights.items()},
            "policy": {"window": policy.window, "min_videos": policy.min_videos},
            "sufficient_evidence": bool(components),
            "videos_considered": len(recent),
        },
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def latest_ip_fitness(session: Session, ip: IP) -> FitnessSnapshot | None:
    return session.scalars(
        select(FitnessSnapshot)
        .where(FitnessSnapshot.scope == FitnessScope.IP, FitnessSnapshot.ip_id == ip.id)
        .order_by(FitnessSnapshot.created_at.desc())
    ).first()
