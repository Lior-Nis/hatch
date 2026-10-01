from enum import StrEnum


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
