from itertools import pairwise

import pytest
from sqlalchemy.orm import Session

from app.experiments.models import Experiment
from app.experiments.states import (
    EXPERIMENT_LIFECYCLE,
    VIDEO_LIFECYCLE,
    ExperimentConclusion,
    ExperimentStatus,
    VideoStatus,
)
from app.ips.models import IP
from app.ips.states import IP_LIFECYCLE, IPStatus
from app.lifecycle import IllegalTransition
from tests.factories import make_experiment

V = VideoStatus
E = ExperimentStatus
I = IPStatus  # noqa: E741

VIDEO_HAPPY_PATH = [
    V.PROPOSED, V.SCRIPTED, V.STORYBOARDED, V.GENERATING, V.GENERATED, V.QA_PENDING,
    V.APPROVAL_PENDING, V.READY, V.SCHEDULED, V.PUBLISHED, V.OBSERVING, V.EVALUATED,
]  # fmt: skip
EXPERIMENT_HAPPY_PATH = [
    E.HYPOTHESIS_CREATED, E.CANDIDATE_CREATED, E.RUNNING, E.OBSERVING, E.EARLY_EVALUATED,
    E.MATURED, E.CONCLUDED,
]  # fmt: skip
IP_HAPPY_PATH = [I.IDEA, I.EXPERIMENTAL, I.PROMISING, I.VALIDATED, I.SCALED]


# --- video ---------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), list(pairwise(VIDEO_HAPPY_PATH)))
def test_video_happy_path_is_legal(current: VideoStatus, target: VideoStatus) -> None:
    VIDEO_LIFECYCLE.check(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (V.GENERATING, V.GENERATION_FAILED),
        (V.QA_PENDING, V.QA_REJECTED),
        (V.APPROVAL_PENDING, V.HUMAN_REJECTED),
        (V.SCHEDULED, V.PUBLISH_FAILED),
        (V.PROPOSED, V.ABORTED_BUDGET),
        (V.GENERATING, V.ABORTED_BUDGET),
        (V.GENERATION_FAILED, V.ABORTED_BUDGET),
        (V.QA_REJECTED, V.ABORTED_BUDGET),
    ],
)
def test_video_failure_states_are_reachable(current: VideoStatus, target: VideoStatus) -> None:
    VIDEO_LIFECYCLE.check(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (V.GENERATION_FAILED, V.GENERATING),  # retry
        (V.QA_REJECTED, V.GENERATING),  # regenerate after automated rejection
        (V.PUBLISH_FAILED, V.SCHEDULED),  # retry publishing
        (V.QA_PENDING, V.READY),  # Stage C: no human approval step
    ],
)
def test_video_recovery_paths_are_legal(current: VideoStatus, target: VideoStatus) -> None:
    VIDEO_LIFECYCLE.check(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (V.GENERATED, V.READY),  # skipping QA
        (V.GENERATED, V.APPROVAL_PENDING),  # skipping QA
        (V.GENERATED, V.PUBLISHED),
        (V.QA_REJECTED, V.READY),  # overriding a QA rejection
        (V.QA_REJECTED, V.APPROVAL_PENDING),
        (V.HUMAN_REJECTED, V.READY),  # overriding a human rejection
        (V.PROPOSED, V.GENERATED),
        (V.READY, V.PUBLISHED),  # publishing without scheduling
        (V.EVALUATED, V.OBSERVING),
        (V.ABORTED_BUDGET, V.GENERATING),  # resuming after a budget abort
        (V.PROPOSED, V.PROPOSED),
    ],
)
def test_illegal_video_transitions_are_rejected(current: VideoStatus, target: VideoStatus) -> None:
    with pytest.raises(IllegalTransition):
        VIDEO_LIFECYCLE.check(current, target)


def test_terminal_video_states_have_no_exits() -> None:
    for state in (V.EVALUATED, V.HUMAN_REJECTED, V.ABORTED_BUDGET):
        assert VIDEO_LIFECYCLE.allowed(state) == frozenset()


def test_every_video_state_is_covered_by_the_lifecycle() -> None:
    assert set(VIDEO_LIFECYCLE.states) == set(VideoStatus)


# --- experiment ----------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), list(pairwise(EXPERIMENT_HAPPY_PATH)))
def test_experiment_happy_path_is_legal(
    current: ExperimentStatus, target: ExperimentStatus
) -> None:
    EXPERIMENT_LIFECYCLE.check(current, target)


@pytest.mark.parametrize(
    "current", [E.CANDIDATE_CREATED, E.RUNNING, E.OBSERVING, E.EARLY_EVALUATED]
)
def test_an_unfinished_experiment_can_be_concluded_early(current: ExperimentStatus) -> None:
    EXPERIMENT_LIFECYCLE.check(current, E.CONCLUDED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (E.HYPOTHESIS_CREATED, E.RUNNING),
        (E.RUNNING, E.MATURED),
        (E.OBSERVING, E.MATURED),
        (E.CONCLUDED, E.RUNNING),
        (E.MATURED, E.OBSERVING),
    ],
)
def test_illegal_experiment_transitions_are_rejected(
    current: ExperimentStatus, target: ExperimentStatus
) -> None:
    with pytest.raises(IllegalTransition):
        EXPERIMENT_LIFECYCLE.check(current, target)


def test_every_experiment_state_is_covered_by_the_lifecycle() -> None:
    assert set(EXPERIMENT_LIFECYCLE.states) == set(ExperimentStatus)


# --- IP ------------------------------------------------------------------


@pytest.mark.parametrize(("current", "target"), list(pairwise(IP_HAPPY_PATH)))
def test_ip_happy_path_is_legal(current: IPStatus, target: IPStatus) -> None:
    IP_LIFECYCLE.check(current, target)


@pytest.mark.parametrize("current", IP_HAPPY_PATH)
def test_any_active_ip_can_be_archived(current: IPStatus) -> None:
    IP_LIFECYCLE.check(current, I.ARCHIVED)


def test_an_archived_ip_can_only_be_resurrected_to_experimental() -> None:
    assert IP_LIFECYCLE.allowed(I.ARCHIVED) == frozenset({I.EXPERIMENTAL})


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (I.IDEA, I.PROMISING),
        (I.EXPERIMENTAL, I.VALIDATED),
        (I.EXPERIMENTAL, I.SCALED),  # one viral video cannot scale an IP
        (I.PROMISING, I.SCALED),
        (I.ARCHIVED, I.SCALED),
        (I.SCALED, I.IDEA),
    ],
)
def test_illegal_ip_transitions_are_rejected(current: IPStatus, target: IPStatus) -> None:
    with pytest.raises(IllegalTransition):
        IP_LIFECYCLE.check(current, target)


# --- enforcement on the models -------------------------------------------


def test_assigning_an_illegal_video_status_on_a_model_raises(session: Session) -> None:
    experiment = make_experiment(session)

    with pytest.raises(IllegalTransition, match=r"proposed.*ready"):
        experiment.video_status = VideoStatus.READY

    assert experiment.video_status is VideoStatus.PROPOSED


def test_enforcement_also_applies_to_rows_loaded_from_the_database(session: Session) -> None:
    experiment_id = make_experiment(session).id
    session.expire_all()
    loaded = session.get_one(Experiment, experiment_id)

    loaded.video_status = VideoStatus.SCRIPTED
    with pytest.raises(IllegalTransition):
        loaded.video_status = VideoStatus.PUBLISHED


def test_a_new_entity_must_start_in_its_initial_state() -> None:
    with pytest.raises(IllegalTransition):
        IP(slug="x", name="X", category="c", spec={}, status=IPStatus.SCALED)


def test_an_experiment_cannot_be_concluded_without_a_conclusion(session: Session) -> None:
    experiment = make_experiment(session)

    with pytest.raises(ValueError, match="conclusion"):
        experiment.status = ExperimentStatus.CONCLUDED

    experiment.conclude(ExperimentConclusion.INCONCLUSIVE)
    assert experiment.status is ExperimentStatus.CONCLUDED
    assert experiment.conclusion is ExperimentConclusion.INCONCLUSIVE


def test_a_conclusion_is_final(session: Session) -> None:
    experiment = make_experiment(session)
    experiment.conclude(ExperimentConclusion.INCONCLUSIVE)

    with pytest.raises(ValueError, match="already concluded"):
        experiment.conclude(ExperimentConclusion.SUPPORTED)


def test_ip_status_assignment_is_enforced(session: Session) -> None:
    ip = make_experiment(session).ip

    ip.status = IPStatus.EXPERIMENTAL
    with pytest.raises(IllegalTransition):
        ip.status = IPStatus.SCALED
