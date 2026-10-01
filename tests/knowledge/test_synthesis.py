from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.candidates import propose_novel
from app.creative.genome import Genes
from app.evolution.policy import EvolutionPolicy
from app.evolution.replication import evaluate_replication, request_replication
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.models import CreativeGenome, Experiment
from app.experiments.service import create_experiment
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.knowledge.models import KnowledgeSummary
from app.knowledge.synthesis import synthesize_ip_knowledge
from app.platforms import Platform
from integrations.fake.llm import FakeLanguageModel
from tests.creative.test_hypotheses import DEFAULTS, novel_draft
from tests.evolution.helpers import evaluated
from tests.factories import LIMITS

POLICY = EvolutionPolicy()


def scored(session: Session, score: float, **genes: object) -> Experiment:
    """An evaluated experiment with some genes different from the fixture."""
    spec = FIRST_SHORT.model_copy(
        update={
            "genome": FIRST_SHORT.genome.model_copy(
                update={
                    "genes": Genes.model_validate(
                        {**FIRST_SHORT.genome.genes.model_dump(), **genes}
                    )
                }
            )
        }
    )
    experiment = create_experiment(session, spec)
    session.add(
        FitnessSnapshot(
            scope=FitnessScope.VIDEO,
            ip_id=experiment.ip_id,
            experiment=experiment,
            platform=Platform.YOUTUBE_SHORTS,
            checkpoint="72h",
            evaluator="heuristic",
            evaluator_version="1",
            score=score,
            components={},
            inputs={"confidence": 1.0},
        )
    )
    session.flush()
    return experiment


def by_topic(session: Session) -> dict[str, KnowledgeSummary]:
    return {summary.topic: summary for summary in session.scalars(select(KnowledgeSummary))}


def test_gene_values_that_outperform_the_rest_become_summaries_with_supporting_experiments(
    session: Session,
) -> None:
    cold = [scored(session, s, hook_type="cold_open") for s in (0.70, 0.74, 0.68)]
    for s in (0.48, 0.52, 0.50):
        scored(session, s, hook_type="visual_question")

    synthesize_ip_knowledge(session, cold[0].ip, POLICY)

    summary = by_topic(session)["gene:hook_type=cold_open"]
    assert "cold_open" in summary.statement and "0.70" in summary.statement
    assert "0.50" in summary.statement  # what it is compared with
    assert {e.id for e in summary.supporting_experiments} == {e.id for e in cold}
    assert 0 < summary.confidence <= 1
    assert summary.ip_id == cold[0].ip_id
    assert "gene:hook_type=visual_question" in by_topic(session)  # the weaker side is knowledge too


def test_no_summary_is_made_from_a_single_video_or_a_negligible_difference(
    session: Session,
) -> None:
    scored(session, 0.9, hook_type="cold_open")  # one video proves nothing
    for s in (0.50, 0.51, 0.50):
        scored(session, s, hook_type="visual_question")
    same = [scored(session, s, pace="brisk") for s in (0.52, 0.50)]

    synthesize_ip_knowledge(session, same[0].ip, POLICY)

    topics = by_topic(session)
    assert "gene:hook_type=cold_open" not in topics
    assert "gene:pace=brisk" not in topics


def test_replication_verdicts_become_knowledge(session: Session) -> None:
    winner = evaluated(session, 0.9)
    request_replication(session, winner, POLICY)
    descendants = [evaluated(session, 0.7, parent=winner) for _ in range(3)]
    evaluate_replication(session, winner, POLICY)

    synthesize_ip_knowledge(session, winner.ip, POLICY)

    summary = by_topic(session)[f"hypothesis:{winner.id}"]
    assert "supported" in summary.statement.lower()
    assert winner.hypothesis.statement in summary.statement
    assert {e.id for e in summary.supporting_experiments} == {
        winner.id, *(d.id for d in descendants)
    }  # fmt: skip
    assert summary.confidence >= 0.8


def test_summaries_are_revised_in_place_as_evidence_changes(session: Session) -> None:
    cold = [scored(session, s, hook_type="cold_open") for s in (0.70, 0.74)]
    for s in (0.50, 0.50):
        scored(session, s, hook_type="visual_question")
    synthesize_ip_knowledge(session, cold[0].ip, POLICY)
    before = by_topic(session)["gene:hook_type=cold_open"]
    first_statement, first_id = before.statement, before.id

    scored(session, 0.30, hook_type="cold_open")
    scored(session, 0.32, hook_type="cold_open")
    synthesize_ip_knowledge(session, cold[0].ip, POLICY)

    after = by_topic(session)["gene:hook_type=cold_open"]
    assert after.id == first_id
    assert after.version == 2
    assert after.statement != first_statement
    assert len(after.supporting_experiments) == 4


def test_synthesis_twice_without_new_evidence_changes_nothing(session: Session) -> None:
    cold = [scored(session, s, hook_type="cold_open") for s in (0.70, 0.74)]
    for s in (0.50, 0.50):
        scored(session, s, hook_type="visual_question")

    synthesize_ip_knowledge(session, cold[0].ip, POLICY)
    synthesize_ip_knowledge(session, cold[0].ip, POLICY)

    assert all(summary.version == 1 for summary in by_topic(session).values())


def test_raw_evidence_is_untouched_by_synthesis(session: Session) -> None:
    cold = [scored(session, s, hook_type="cold_open") for s in (0.70, 0.74)]
    for s in (0.50, 0.50):
        scored(session, s, hook_type="visual_question")
    genomes = {g.id: dict(g.genes) for g in session.scalars(select(CreativeGenome))}
    fitness_rows = session.scalar(select(func.count()).select_from(FitnessSnapshot))

    synthesize_ip_knowledge(session, cold[0].ip, POLICY)
    session.expire_all()

    assert {g.id: dict(g.genes) for g in session.scalars(select(CreativeGenome))} == genomes
    assert session.scalar(select(func.count()).select_from(FitnessSnapshot)) == fitness_rows


def test_creative_generation_reads_the_summaries(session: Session) -> None:
    cold = [scored(session, s, hook_type="cold_open") for s in (0.70, 0.74)]
    for s in (0.50, 0.50):
        scored(session, s, hook_type="visual_question")
    synthesize_ip_knowledge(session, cold[0].ip, POLICY)
    llm = FakeLanguageModel([novel_draft()])

    propose_novel(
        session, cold[0].ip, llm=llm, governor=BudgetGovernor(LIMITS),
        output=FIRST_SHORT.output, production=DEFAULTS,
    )  # fmt: skip

    statement = by_topic(session)["gene:hook_type=cold_open"].statement
    assert statement in llm.requests[0].prompt


def test_production_knowledge_reports_cost_per_accepted_video_by_model(session: Session) -> None:
    from app.budgets.models import BudgetLedgerEntry, LedgerStatus
    from app.experiments.states import VideoStatus
    from tests.factories import force_video_status

    accepted = scored(session, 0.6)
    rejected = scored(session, 0.6)
    force_video_status(session, accepted, VideoStatus.PUBLISHED)
    force_video_status(session, rejected, VideoStatus.QA_REJECTED)
    for experiment in (accepted, rejected):
        session.add(
            BudgetLedgerEntry(
                provider="higgsfield",
                model="alibaba/wan-3.0/text-to-video",
                operation="text_to_video",
                experiment_id=experiment.id,
                estimated_cost_usd=Decimal("0.40"),
                actual_cost_usd=Decimal("0.40"),
                status=LedgerStatus.SETTLED,
            )
        )
    session.flush()

    synthesize_ip_knowledge(session, accepted.ip, POLICY)

    summary = by_topic(session)["production:video_model=alibaba/wan-3.0/text-to-video"]
    assert "$0.80 per accepted video" in summary.statement
    assert "1 of 2" in summary.statement
