"""Queue jobs for the evolution cycle."""

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evolution.cycle import evolve_ip
from app.evolution.policy import EvolutionPolicy
from app.ips.models import IP
from app.scheduling.models import JobRun
from app.scheduling.queue import enqueue
from app.scheduling.recurring import ensure_recurring, window_start
from app.scheduling.worker import JobHandler, JobResult

EVOLVE_IP = "evolve_ip"
EVOLUTION_CYCLE = "evolution_cycle"


def enqueue_evolution(
    session: Session, ip_id: uuid.UUID, *, now: datetime | None = None, window: str = ""
) -> JobRun:
    return enqueue(
        session,
        EVOLVE_IP,
        payload={"ip_id": str(ip_id)},
        idempotency_key=f"{EVOLVE_IP}:{ip_id}:{window}" if window else None,
        now=now,
    )


def evolution_handlers(
    policy: EvolutionPolicy,
    *,
    pipeline_target: int,
    interval: timedelta = timedelta(hours=6),
) -> dict[str, JobHandler]:
    def evolve(session: Session, job: JobRun) -> JobResult:
        ip = session.get_one(IP, uuid.UUID(job.payload["ip_id"]))
        report = evolve_ip(session, ip, policy, pipeline_target=pipeline_target, now=job.started_at)
        return asdict(report)

    def cycle(session: Session, job: JobRun) -> JobResult:
        """Queue one evolution run per IP for this window, then make sure the
        next window's cycle exists."""
        now = job.started_at
        assert now is not None
        window = window_start(now, interval).isoformat()
        ips = session.scalars(select(IP).order_by(IP.slug)).all()
        for ip in ips:
            enqueue_evolution(session, ip.id, now=now, window=window)
        ensure_recurring(session, EVOLUTION_CYCLE, interval=interval, now=now + interval)
        return {"ips": len(ips), "window": window}

    return {EVOLVE_IP: evolve, EVOLUTION_CYCLE: cycle}
