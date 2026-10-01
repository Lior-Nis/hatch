"""The V1 closed loop, end to end, with fake vendors and a controlled clock:

    experiment memory → hypothesis → genome → generation → automated QA →
    human approval → publishing → platform analytics → video fitness →
    IP fitness → exploit/mutate/explore decision → next experiment

The only human action is the approval. No creative instruction is given.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.jobs import analytics_handlers
from app.budgets.governor import BudgetGovernor
from app.creative.candidates import CandidateDraft
from app.creative.jobs import creative_handlers
from app.evolution.jobs import enqueue_evolution, evolution_handlers
from app.evolution.models import AllocationBucket, DecisionType, ParentRelation, SelectionDecision
from app.evolution.policy import EvolutionPolicy
from app.experiments.fixtures import FIRST_SHORT
from app.experiments.lineage import get_lineage
from app.experiments.models import Experiment
from app.experiments.service import get_or_create_ip
from app.experiments.states import ExperimentStatus, VideoStatus
from app.fitness.heuristic import HeuristicFitnessEvaluator
from app.fitness.jobs import fitness_handlers
from app.fitness.models import FitnessScope, FitnessSnapshot
from app.ips.states import IPStatus
from app.platforms import Platform
from app.production.jobs import PRODUCE_SHORT, produce_short_handler
from app.production.run import ProductionDeps
from app.publishing.jobs import publishing_handlers
from app.publishing.models import Publication
from app.publishing.schedule import PostingSchedule
from app.quality.jobs import RUN_QA, run_qa_handler
from app.quality.models import ReviewDecision
from app.quality.review import submit_review
from app.quality.technical import TechnicalQAGate
from app.scheduling.models import JobRun, JobStatus
from app.scheduling.worker import Worker
from integrations.fake.analytics import FakeAnalyticsAdapter
from integrations.fake.llm import FakeLanguageModel
from integrations.fake.media import FakeMediaGenerator
from integrations.fake.publisher import FakePublisher
from integrations.object_storage.local import LocalAssetStore
from tests.creative.test_hypotheses import DEFAULTS, creative_genes, draft
from tests.factories import LIMITS
from tests.publishing.test_service import CHANNELS, map_all

START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
STRONG = {
    "views": 4000, "average_watch_fraction": 0.92, "completion_rate": 0.6,
    "likes": 260, "shares": 40, "saves": 45, "comments": 12, "follows": 30,
}  # fmt: skip
STORIES = [
    ("why moss feels soft", "Nib presses a paw into a moss cushion and it springs back."),
    ("how rain makes music", "Rain taps three acorn cups and each sings a different note."),
    ("where snails sleep", "A snail curls into its shell under a mushroom as dusk falls."),
    ("why leaves turn gold", "A green leaf drifts down and turns gold in Nib's paws."),
    ("what owls hear at night", "An owl turns its head as tiny footsteps cross the hollow."),
]


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class LiveAnalytics(FakeAnalyticsAdapter):
    """Reports the same strong metrics for any post on its platform."""

    def __init__(self, platform: Platform) -> None:
        super().__init__(platform=platform, payloads={})

    def fetch_post_metrics(self, *, platform_account_id: str, platform_post_id: str) -> Any:
        self._payloads[platform_post_id] = STRONG
        return super().fetch_post_metrics(
            platform_account_id=platform_account_id, platform_post_id=platform_post_id
        )


def story_draft(index: int, **overrides: Any) -> CandidateDraft:
    topic, spec = STORIES[index]
    return draft(
        creative_genes=creative_genes(topic=topic, educational_goal=None),
        creative_spec=spec,
        **overrides,
    )


def test_the_loop_closes_from_evidence_to_the_next_experiment(
    session: Session, tmp_path: Path
) -> None:
    clock = Clock()
    ip = get_or_create_ip(session, FIRST_SHORT.ip)
    map_all(session, ip)
    store = LocalAssetStore(tmp_path / "assets", public_base_url="https://media.example.com")
    generator = FakeMediaGenerator(cost_per_second_usd=Decimal("0.05"))
    publisher = FakePublisher(accounts=set(CHANNELS.values()))
    llm = FakeLanguageModel([story_draft(i) for i in range(len(STORIES))])
    governor = BudgetGovernor(LIMITS)
    policy = EvolutionPolicy()

    @contextmanager
    def sessions() -> Iterator[Session]:
        yield session

    handlers = {
        PRODUCE_SHORT: produce_short_handler(
            ProductionDeps(generator=generator, store=store, governor=governor),
            poll_interval=timedelta(seconds=5),
        ),
        RUN_QA: run_qa_handler(gates=[TechnicalQAGate()], store=store),
        **creative_handlers(
            llm=llm, governor=governor, output=FIRST_SHORT.output, production=DEFAULTS
        ),
        **publishing_handlers(
            publisher=publisher,
            store=store,
            schedule=PostingSchedule(),
            poll_interval=timedelta(minutes=15),
        ),
        **analytics_handlers({platform: LiveAnalytics(platform) for platform in Platform}),
        **fitness_handlers(HeuristicFitnessEvaluator(), target_cost_usd=Decimal("0.50")),
        **evolution_handlers(policy, pipeline_target=1),
    }
    worker = Worker(sessions, handlers, worker_id="loop", clock=clock)

    def run() -> None:
        for _ in range(300):
            if not worker.run_once():
                return
        raise AssertionError("queue did not drain")

    # 1. With no evidence at all, the system explores: it proposes, produces
    #    and checks a first video by itself.
    enqueue_evolution(session, ip.id, now=clock())
    run()
    first = session.scalars(select(Experiment)).one()
    assert first.video_status is VideoStatus.APPROVAL_PENDING
    assert first.hypothesis.source == "creative_agent:fake-llm-1"
    [exploration] = session.scalars(select(SelectionDecision)).all()
    assert exploration.bucket is AllocationBucket.EXPLORE
    assert exploration.resulting_experiment_id == first.id

    # 2. The one human step: approval. The publishing cycle then schedules it.
    submit_review(session, first.id, decision=ReviewDecision.APPROVE, reason="", reviewer="lior")
    from app.publishing.jobs import schedule_ready_videos

    schedule_ready_videos(session, now=clock())
    run()
    assert session.get_one(Experiment, first.id).video_status is VideoStatus.SCHEDULED

    # 3. The platforms publish; Hatch records post ids and starts observing.
    posted = clock.now = datetime(2026, 10, 1, 15, 5, tzinfo=UTC)
    for publication in session.scalars(select(Publication)):
        publisher.deliver(publication.external_id or "")
        publication.published_at = posted
    run()
    for publication in session.scalars(select(Publication)):
        publication.published_at = posted  # the fake publisher stamps real time
    session.flush()

    # 4. Three days later the evidence is in: observations, video fitness, IP fitness.
    clock.now = posted + timedelta(hours=73)
    run()
    session.expire_all()
    first = session.get_one(Experiment, first.id)
    assert first.status is ExperimentStatus.EARLY_EVALUATED
    video_scores = session.scalars(
        select(FitnessSnapshot).where(FitnessSnapshot.scope == FitnessScope.VIDEO)
    ).all()
    assert {f.platform for f in video_scores} == set(Platform)
    assert all(f.score > 0.65 for f in video_scores)
    assert session.scalars(
        select(FitnessSnapshot).where(FitnessSnapshot.scope == FitnessScope.IP)
    ).first()

    # 5. The next cycle acts on that evidence with no instruction from anyone:
    #    the strong video is a potential winner, so replication is requested and
    #    a descendant with the same mechanism and a new story is created.
    enqueue_evolution(session, ip.id, now=clock())
    run()
    session.expire_all()
    request = session.scalars(
        select(SelectionDecision).where(
            SelectionDecision.decision_type == DecisionType.REQUEST_REPLICATION
        )
    ).one()
    assert request.subject_experiment_id == first.id
    assert request.evidence["score"] > 0.65
    second = session.scalars(
        select(Experiment).where(Experiment.id != first.id).order_by(Experiment.created_at)
    ).first()
    assert second is not None
    [link] = second.parents
    assert (link.parent_id, link.relation) == (first.id, ParentRelation.REPLICATION)
    assert second.lineage_id == first.lineage_id
    assert second.genome.genes["hook_type"] == first.genome.genes["hook_type"]  # same mechanism
    assert second.genome.genes["topic"] != first.genome.genes["topic"]  # new story
    assert second.video_status is VideoStatus.APPROVAL_PENDING  # produced and checked already
    creation = session.scalars(
        select(SelectionDecision).where(SelectionDecision.resulting_experiment_id == second.id)
    ).one()
    assert creation.bucket is AllocationBucket.MUTATE
    assert creation.evidence["relation"] == "replication"
    assert session.get_one(type(ip), ip.id).status is IPStatus.PROMISING

    # 6. The whole causal chain of the new video is inspectable.
    lineage = get_lineage(session, second.id)
    assert lineage.experiment.parent_experiment_ids == [first.id]
    assert lineage.hypothesis.prediction["evidence_experiment_ids"] == [str(first.id)]
    assert lineage.final_asset is not None and lineage.cost.committed_usd > 0
    failed = session.scalars(select(JobRun).where(JobRun.status == JobStatus.FAILED)).all()
    assert failed == []
