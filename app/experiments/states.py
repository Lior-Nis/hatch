from enum import StrEnum

from app.lifecycle import Lifecycle


class ExperimentStatus(StrEnum):
    HYPOTHESIS_CREATED = "hypothesis_created"
    CANDIDATE_CREATED = "candidate_created"
    RUNNING = "running"
    OBSERVING = "observing"
    EARLY_EVALUATED = "early_evaluated"
    MATURED = "matured"
    CONCLUDED = "concluded"


class ExperimentConclusion(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    NOT_SUPPORTED = "not_supported"
    INCONCLUSIVE = "inconclusive"
    ANOMALOUS = "anomalous"


class VideoStatus(StrEnum):
    PROPOSED = "proposed"
    SCRIPTED = "scripted"
    STORYBOARDED = "storyboarded"
    GENERATING = "generating"
    GENERATED = "generated"
    QA_PENDING = "qa_pending"
    APPROVAL_PENDING = "approval_pending"
    READY = "ready"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    OBSERVING = "observing"
    EVALUATED = "evaluated"
    # Failure states
    GENERATION_FAILED = "generation_failed"
    QA_REJECTED = "qa_rejected"
    HUMAN_REJECTED = "human_rejected"
    PUBLISH_FAILED = "publish_failed"
    ABORTED_BUDGET = "aborted_budget"


PUBLISHED_VIDEO_STATES = frozenset(
    {VideoStatus.PUBLISHED, VideoStatus.OBSERVING, VideoStatus.EVALUATED}
)
ACCEPTED_VIDEO_STATES = PUBLISHED_VIDEO_STATES | {
    VideoStatus.READY,
    VideoStatus.SCHEDULED,
    VideoStatus.PUBLISH_FAILED,
}
"""Videos that passed automated QA and (in Stage A/B) human approval."""

_E = ExperimentStatus
EXPERIMENT_LIFECYCLE = Lifecycle(
    "experiment",
    initial=_E.HYPOTHESIS_CREATED,
    transitions={
        _E.HYPOTHESIS_CREATED: {_E.CANDIDATE_CREATED},
        # An experiment that never yields observable evidence (rejected or
        # failed video, budget abort, removed post) is concluded early.
        _E.CANDIDATE_CREATED: {_E.RUNNING, _E.CONCLUDED},
        _E.RUNNING: {_E.OBSERVING, _E.CONCLUDED},
        _E.OBSERVING: {_E.EARLY_EVALUATED, _E.CONCLUDED},
        _E.EARLY_EVALUATED: {_E.MATURED, _E.CONCLUDED},
        _E.MATURED: {_E.CONCLUDED},
        _E.CONCLUDED: set(),
    },
)

_V = VideoStatus
VIDEO_LIFECYCLE = Lifecycle(
    "video",
    initial=_V.PROPOSED,
    transitions={
        _V.PROPOSED: {_V.SCRIPTED, _V.ABORTED_BUDGET},
        _V.SCRIPTED: {_V.STORYBOARDED, _V.ABORTED_BUDGET},
        _V.STORYBOARDED: {_V.GENERATING, _V.ABORTED_BUDGET},
        _V.GENERATING: {_V.GENERATED, _V.GENERATION_FAILED, _V.ABORTED_BUDGET},
        # A generated video can only move forward through automated QA.
        _V.GENERATED: {_V.QA_PENDING},
        # QA_PENDING → READY is the Stage C path (no human approval step).
        _V.QA_PENDING: {_V.APPROVAL_PENDING, _V.READY, _V.QA_REJECTED},
        _V.APPROVAL_PENDING: {_V.READY, _V.HUMAN_REJECTED},
        _V.READY: {_V.SCHEDULED},
        _V.SCHEDULED: {_V.PUBLISHED, _V.PUBLISH_FAILED, _V.READY},
        _V.PUBLISHED: {_V.OBSERVING},
        _V.OBSERVING: {_V.EVALUATED},
        _V.EVALUATED: set(),
        # Failure states. Retries regenerate; the rejected asset stays rejected
        # and the new one must pass QA from the start.
        _V.GENERATION_FAILED: {_V.GENERATING, _V.ABORTED_BUDGET},
        _V.QA_REJECTED: {_V.GENERATING, _V.ABORTED_BUDGET},
        _V.HUMAN_REJECTED: set(),
        _V.PUBLISH_FAILED: {_V.SCHEDULED},
        _V.ABORTED_BUDGET: set(),
    },
)
