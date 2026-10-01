"""Model-based content QA.

One multimodal review of sampled frames feeds four mandatory gates:

- ``child_safety`` — hard gate. A clear finding in any category FAILS; a
  possible one ESCALATES to a human. Nothing else in the review (creative
  scores, "this will perform well") can soften it.
- ``visual``       — character drift, continuity, malformed visuals, garbled text.
- ``ip_brand``     — logos, brands, resemblance to known characters: ESCALATES.
- ``creative``     — story clarity, age appropriateness, pacing, ending, and
  whether the video embodies its hypothesis.

The model reports observations; Hatch applies fixed, configurable thresholds,
so every decision is reproducible from the stored review. If the review cannot
be obtained the gates raise, which the QA runner records as ESCALATE — an
unreviewed video never passes.

Limits: the review sees still frames only. It does not hear audio or see
motion between frames; Stage A human approval covers that gap.
"""

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.ports import LLMError, LLMRequest
from app.quality.frames import sample_frames
from app.quality.ports import QACandidate, QAGate, QAOutcome, QAVerdict

CONTENT_QA_VERSION = "content-1"

SAFETY_CATEGORIES = (
    "sexual_content",
    "graphic_violence",
    "dangerous_imitation",
    "self_harm",
    "inappropriate_fear",
    "abusive_conduct",
    "unsafe_challenge",
    "adult_themes",
)


class SafetyFinding(BaseModel):
    category: str
    level: Literal["none", "possible", "clear"]
    evidence: str


class VisualReview(BaseModel):
    character_consistency: float
    continuity: float
    visual_integrity: float
    free_of_garbled_text: float
    notes: str


class IPReview(BaseModel):
    logos_or_brands: bool
    resembles_known_character: bool
    resembles: str
    notes: str


class CreativeReview(BaseModel):
    story_clarity: float
    age_appropriateness: float
    pacing: float
    ending_clarity: float
    hypothesis_alignment: float
    notes: str


class ContentReview(BaseModel):
    """What the reviewing model returns."""

    safety: list[SafetyFinding]
    visual: VisualReview
    ip: IPReview
    creative: CreativeReview
    summary: str


class ContentThresholds(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_visual_score: float = Field(default=0.5, ge=0, le=1)
    min_age_appropriateness: float = Field(default=0.6, ge=0, le=1)
    min_hypothesis_alignment: float = Field(default=0.4, ge=0, le=1)
    min_creative_score: float = Field(default=0.3, ge=0, le=1)


ReviewCaller = Callable[[LLMRequest, type[ContentReview], str | None], ContentReview]
"""(request, schema, experiment_id) → the parsed review. Bound to a database
session and budget governor by the caller."""

_SYSTEM = f"""\
You review short vertical videos made for children aged 4 to 8 before they can be \
published. You are shown still frames sampled evenly across one video, in order, followed \
by what the video was meant to be.

Report only what you can see. Be honest and specific; when you are unsure, say so by \
choosing the more cautious option. Your observations are checked against fixed rules, so \
do not adjust them to reach a verdict.

Safety. For each of these categories give a level — none, possible, or clear — and the \
evidence (which frame, what is visible): {", ".join(SAFETY_CATEGORIES)}.
- dangerous_imitation: anything a young child could copy and get hurt (climbing, sharp or \
hot things, water, small objects in mouths, entering appliances or enclosed spaces).
- inappropriate_fear: threatening, grotesque or distressing imagery, including distorted \
faces or bodies produced by generation errors.
Include every category exactly once.

Visual quality, each from 0.0 (unusable) to 1.0 (clean):
- character_consistency: characters match their reference description and stay the same.
- continuity: setting, lighting and objects stay coherent from frame to frame.
- visual_integrity: no malformed anatomy, melting shapes or corrupted regions.
- free_of_garbled_text: 1.0 when there is no text or it is clean; lower for garbled text.

IP and brand: say whether any logo, brand or trademark is visible, and whether any \
character resembles a well-known existing character (name it).

Creative, each from 0.0 to 1.0: story_clarity (can a child tell what happens), \
age_appropriateness (for ages 4 to 8), pacing, ending_clarity (does it end understandably), \
hypothesis_alignment (does the video actually do what the hypothesis is testing).
"""


class ContentReviewer:
    """Obtains one ``ContentReview`` per video and shares it between gates."""

    def __init__(
        self, call: ReviewCaller | None, *, frame_count: int = 8, unavailable: str = ""
    ) -> None:
        """Pass ``call=None`` with an ``unavailable`` reason when no model is
        configured: every review then fails, so the gates escalate."""
        self._call = call
        self._unavailable = unavailable or "content review is not available"
        self._frame_count = frame_count
        self._cache: dict[tuple[str, str], tuple[ContentReview, list[float]] | LLMError] = {}

    def review(self, candidate: QACandidate) -> tuple[ContentReview, list[float]]:
        """The review and the timestamps of the frames it was based on."""
        key = (candidate.experiment_id, str(candidate.media_path))
        if key not in self._cache:
            try:
                self._cache[key] = self._review(candidate)
            except LLMError as exc:
                self._cache[key] = exc  # do not pay again for each remaining gate
        cached = self._cache[key]
        if isinstance(cached, LLMError):
            raise cached
        return cached

    def _review(self, candidate: QACandidate) -> tuple[ContentReview, list[float]]:
        if self._call is None:
            raise LLMError(self._unavailable)
        ip = candidate.ip_spec
        context: dict[str, Any] = {
            "creative_spec": candidate.creative_spec,
            "hypothesis": candidate.hypothesis,
            "characters": ip.get("characters", []),
            "world_rules": ip.get("world_rules", []),
            "safety_constraints": ip.get("safety_constraints", []),
            "visual_identity": ip.get("visual_identity"),
            "genes": candidate.genes,
        }
        with tempfile.TemporaryDirectory(prefix="hatch-qa-") as workdir:
            frames = sample_frames(candidate.media_path, Path(workdir), count=self._frame_count)
            request = LLMRequest(
                purpose="qa_content_review",
                system=_SYSTEM,
                prompt=(
                    f"The {len(frames)} frames above are in order. This is what the video was "
                    f"meant to be:\n{json.dumps(context, indent=2, ensure_ascii=False)}"
                ),
                images=tuple(frame.path for frame in frames),
                max_output_tokens=6000,
            )
            review = self._call(request, ContentReview, candidate.experiment_id)
        return review, [round(frame.at_seconds, 2) for frame in frames]


class _ContentGate:
    name = ""
    version = CONTENT_QA_VERSION
    mandatory = True

    def __init__(self, reviewer: ContentReviewer, thresholds: ContentThresholds) -> None:
        self._reviewer = reviewer
        self._thresholds = thresholds

    def evaluate(self, candidate: QACandidate) -> QAVerdict:
        review, frame_times = self._reviewer.review(candidate)
        outcome, scores, reasons, details = self._judge(review)
        return QAVerdict(
            gate=self.name,
            gate_version=self.version,
            outcome=outcome,
            scores=scores,
            reasons=tuple(reasons),
            details={**details, "frames_at_seconds": frame_times, "summary": review.summary},
        )

    def _judge(
        self, review: ContentReview
    ) -> tuple[QAOutcome, dict[str, float], list[str], dict[str, Any]]:
        raise NotImplementedError


class ChildSafetyGate(_ContentGate):
    name = "child_safety"

    def _judge(
        self, review: ContentReview
    ) -> tuple[QAOutcome, dict[str, float], list[str], dict[str, Any]]:
        by_category = {finding.category: finding for finding in review.safety}
        clear = [f for f in review.safety if f.level == "clear"]
        possible = [f for f in review.safety if f.level == "possible"]
        missing = [c for c in SAFETY_CATEGORIES if c not in by_category]
        details = {"findings": [finding.model_dump() for finding in review.safety]}
        if clear:
            return (
                QAOutcome.FAIL,
                {},
                [f"{f.category}: {f.evidence or 'clear finding'}" for f in clear],
                details,
            )
        if possible or missing:
            reasons = [f"{f.category} (possible): {f.evidence or 'uncertain'}" for f in possible]
            reasons += [f"{category}: not assessed by the reviewer" for category in missing]
            return QAOutcome.ESCALATE, {}, reasons, details
        return QAOutcome.PASS, {}, [], details


class VisualQAGate(_ContentGate):
    name = "visual"

    def _judge(
        self, review: ContentReview
    ) -> tuple[QAOutcome, dict[str, float], list[str], dict[str, Any]]:
        scores = review.visual.model_dump(exclude={"notes"})
        low = {k: v for k, v in scores.items() if v < self._thresholds.min_visual_score}
        reasons = [
            f"{name} {value:.2f} is below {self._thresholds.min_visual_score:.2f}"
            + (f": {review.visual.notes}" if review.visual.notes else "")
            for name, value in low.items()
        ]
        outcome = QAOutcome.FAIL if low else QAOutcome.PASS
        return outcome, scores, reasons, {"notes": review.visual.notes}


class IPBrandGate(_ContentGate):
    name = "ip_brand"

    def _judge(
        self, review: ContentReview
    ) -> tuple[QAOutcome, dict[str, float], list[str], dict[str, Any]]:
        reasons = []
        if review.ip.resembles_known_character:
            reasons.append(
                f"resembles a known character ({review.ip.resembles or 'unnamed'}): "
                f"{review.ip.notes}"
            )
        if review.ip.logos_or_brands:
            reasons.append(f"logo or brand visible: {review.ip.notes}")
        # Flagged cases need a human decision: they stay blocked until resolved.
        outcome = QAOutcome.ESCALATE if reasons else QAOutcome.PASS
        return outcome, {}, reasons, review.ip.model_dump()


class CreativeQAGate(_ContentGate):
    name = "creative"

    def _judge(
        self, review: ContentReview
    ) -> tuple[QAOutcome, dict[str, float], list[str], dict[str, Any]]:
        scores = review.creative.model_dump(exclude={"notes"})
        minimums = dict.fromkeys(scores, self._thresholds.min_creative_score)
        minimums["age_appropriateness"] = self._thresholds.min_age_appropriateness
        minimums["hypothesis_alignment"] = self._thresholds.min_hypothesis_alignment
        reasons = [
            f"{name} {value:.2f} is below {minimums[name]:.2f}"
            + (f": {review.creative.notes}" if review.creative.notes else "")
            for name, value in scores.items()
            if value < minimums[name]
        ]
        outcome = QAOutcome.FAIL if reasons else QAOutcome.PASS
        return outcome, scores, reasons, {"notes": review.creative.notes}


def content_gates(reviewer: ContentReviewer, thresholds: ContentThresholds) -> list[QAGate]:
    """The four content gates, sharing one review per video. Safety first."""
    return [
        ChildSafetyGate(reviewer, thresholds),
        VisualQAGate(reviewer, thresholds),
        IPBrandGate(reviewer, thresholds),
        CreativeQAGate(reviewer, thresholds),
    ]
