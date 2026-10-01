"""Knowledge synthesis: turn immutable experiment records into the current,
revisable interpretation that creative agents read.

Three kinds of summary, each citing the experiments it rests on:

- ``gene:<gene>=<value>``   how videos with a mechanism gene value did against
                            the rest of the IP (medians, so one outlier does
                            not invent a finding);
- ``hypothesis:<id>``       the verdict of a replicated hypothesis;
- ``production:video_model=<model>``  cost per accepted video for a model.

Summaries are derived, so they are updated in place (version + 1) when new
evidence changes them. Synthesis never writes to experiment evidence.
"""

import statistics
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budgets.models import BudgetLedgerEntry, committed_usd, is_spend
from app.creative.genome import GENE_SPECS, GeneKind
from app.evolution.evidence import experiment_evidence
from app.evolution.models import DecisionType, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import replication_descendants
from app.experiments.models import Experiment
from app.experiments.states import ACCEPTED_VIDEO_STATES, ExperimentConclusion
from app.ips.models import IP
from app.knowledge.models import KnowledgeSummary

MIN_VIDEOS_PER_SIDE = 2
MIN_DIFFERENCE = 0.05

_VERDICT_CONFIDENCE = {
    ExperimentConclusion.SUPPORTED: 0.9,
    ExperimentConclusion.NOT_SUPPORTED: 0.8,
    ExperimentConclusion.ANOMALOUS: 0.7,
    ExperimentConclusion.PARTIALLY_SUPPORTED: 0.6,
    ExperimentConclusion.INCONCLUSIVE: 0.3,
}

Finding = tuple[str, float, list[Experiment], bool]
"""(statement, confidence, supporting experiments, worth creating). A finding
that is not worth creating (no clear difference) only revises a summary that
already exists: an earlier belief that no longer holds must be corrected."""


def _gene_findings(scored: list[tuple[Experiment, float]]) -> dict[str, Finding]:
    findings: dict[str, Finding] = {}
    for gene, spec in GENE_SPECS.items():
        if not spec.mechanism or spec.kind is not GeneKind.CATEGORICAL:
            continue
        by_value: dict[str, list[tuple[Experiment, float]]] = {}
        for experiment, score in scored:
            by_value.setdefault(str(experiment.genome.genes.get(gene)), []).append(
                (experiment, score)
            )
        for value, group in by_value.items():
            rest = [score for experiment, score in scored if (experiment, score) not in group]
            if len(group) < MIN_VIDEOS_PER_SIDE or len(rest) < MIN_VIDEOS_PER_SIDE:
                continue
            ours = statistics.median(score for _, score in group)
            others = statistics.median(rest)
            difference = ours - others
            comparison = (
                f"{gene}={value}: median fitness {ours:.2f} over {len(group)} videos, versus "
                f"{others:.2f} for other {gene} values ({len(rest)} videos)"
            )
            clear = abs(difference) >= MIN_DIFFERENCE
            if clear:
                direction = "better" if difference > 0 else "worse"
                statement = f"{comparison} — {direction} by {abs(difference):.2f}."
                confidence = min(1.0, len(group) / 5) * min(1.0, abs(difference) / 0.15)
            else:
                statement = f"{comparison} — no clear difference."
                confidence = 0.1
            findings[f"gene:{gene}={value}"] = (
                statement,
                round(confidence, 3),
                [experiment for experiment, _ in group],
                clear,
            )
    return findings


def _verdict_findings(session: Session, ip: IP) -> dict[str, Finding]:
    findings: dict[str, Finding] = {}
    for decision in session.scalars(
        select(SelectionDecision).where(
            SelectionDecision.decision_type == DecisionType.CONCLUDE_EXPERIMENT,
            SelectionDecision.ip_id == ip.id,
        )
    ):
        experiment = decision.subject_experiment
        if experiment is None or experiment.conclusion is None:
            continue
        if "descendants" not in decision.evidence:
            continue  # concluded without replication: not a tested claim
        descendants = replication_descendants(session, experiment)
        tested = len(decision.evidence["descendants"])
        statement = (
            f"{experiment.conclusion.value.replace('_', ' ').upper()} under replication: "
            f"{experiment.hypothesis.statement} — {decision.evidence['successes']} of {tested} "
            f"descendants reached fitness {decision.evidence['support_threshold']:.2f}."
        )
        findings[f"hypothesis:{experiment.id}"] = (
            statement,
            _VERDICT_CONFIDENCE[experiment.conclusion],
            [experiment, *descendants],
            True,
        )
    return findings


def _production_findings(session: Session, ip: IP) -> dict[str, Finding]:
    spend: dict[Any, Decimal] = {
        experiment_id: Decimal(total)
        for experiment_id, total in session.execute(
            select(BudgetLedgerEntry.experiment_id, func.sum(committed_usd()))
            .join(Experiment, Experiment.id == BudgetLedgerEntry.experiment_id)
            .where(is_spend(), Experiment.ip_id == ip.id)
            .group_by(BudgetLedgerEntry.experiment_id)
        )
    }
    by_model: dict[str, list[Experiment]] = {}
    for experiment in session.scalars(select(Experiment).where(Experiment.id.in_(list(spend)))):
        by_model.setdefault(str(experiment.genome.genes.get("video_model")), []).append(experiment)
    findings: dict[str, Finding] = {}
    for model, experiments in by_model.items():
        total = sum((spend[e.id] for e in experiments), Decimal("0"))
        accepted = sum(1 for e in experiments if e.video_status in ACCEPTED_VIDEO_STATES)
        per_accepted = (
            f"${total / accepted:.2f} per accepted video" if accepted else "none accepted"
        )
        findings[f"production:video_model={model}"] = (
            f"{model}: {per_accepted} ({accepted} of {len(experiments)} videos accepted, "
            f"${total:.2f} spent).",
            round(min(1.0, len(experiments) / 10), 3),
            experiments,
            True,
        )
    return findings


def synthesize_ip_knowledge(
    session: Session, ip: IP, policy: EvolutionPolicy
) -> list[KnowledgeSummary]:
    """Recompute the IP's summaries. Returns those created or revised."""
    scored = []
    for experiment in session.scalars(
        select(Experiment).where(Experiment.ip_id == ip.id).order_by(Experiment.created_at)
    ):
        evidence = experiment_evidence(session, experiment, policy)
        if evidence.sufficient and evidence.score is not None:
            scored.append((experiment, evidence.score))

    findings = {
        **_gene_findings(scored),
        **_verdict_findings(session, ip),
        **_production_findings(session, ip),
    }
    existing = {
        summary.topic: summary
        for summary in session.scalars(
            select(KnowledgeSummary).where(KnowledgeSummary.ip_id == ip.id)
        )
    }
    changed = []
    for topic, (statement, confidence, supporting, worth_creating) in findings.items():
        summary = existing.get(topic)
        if summary is None:
            if not worth_creating:
                continue
            summary = KnowledgeSummary(
                ip=ip,
                topic=topic,
                statement=statement,
                confidence=confidence,
                supporting_experiments=supporting,
            )
            session.add(summary)
            changed.append(summary)
        elif summary.statement != statement or {e.id for e in summary.supporting_experiments} != {
            e.id for e in supporting
        }:
            summary.statement = statement
            summary.confidence = confidence
            summary.supporting_experiments = supporting
            summary.version += 1
            changed.append(summary)
    session.flush()
    return changed
