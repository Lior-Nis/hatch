"""Model-based content QA: one multimodal review, four gates."""

from pathlib import Path
from typing import Any

import pytest

from app.experiments.fixtures import FIRST_SHORT
from app.llm.ports import LLMError, LLMRequest
from app.quality.content import (
    SAFETY_CATEGORIES,
    ContentReview,
    ContentReviewer,
    ContentThresholds,
    content_gates,
)
from app.quality.frames import sample_frames
from app.quality.ports import QACandidate, QAGate, QAOutcome, QAVerdict
from integrations.fake.llm import FakeLanguageModel
from integrations.fake.media import render_test_video


def review(**overrides: Any) -> ContentReview:
    fields: dict[str, Any] = {
        "safety": [{"category": c, "level": "none", "evidence": ""} for c in SAFETY_CATEGORIES],
        "visual": {
            "character_consistency": 0.9,
            "continuity": 0.9,
            "visual_integrity": 0.9,
            "free_of_garbled_text": 1.0,
            "notes": "",
        },
        "ip": {
            "logos_or_brands": False,
            "resembles_known_character": False,
            "resembles": "",
            "notes": "",
        },
        "creative": {
            "story_clarity": 0.8,
            "age_appropriateness": 0.95,
            "pacing": 0.8,
            "ending_clarity": 0.8,
            "hypothesis_alignment": 0.8,
            "notes": "",
        },
        "summary": "A gentle, clear short.",
    }
    fields.update(overrides)
    return ContentReview.model_validate(fields)


def safety(**levels: str) -> list[dict[str, str]]:
    return [
        {"category": c, "level": levels.get(c, "none"), "evidence": "seen at 0:05"}
        for c in SAFETY_CATEGORIES
    ]


@pytest.fixture(scope="module")
def video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("qa") / "short.mp4"
    render_test_video(path, width=270, height=480, duration_seconds=4.0)
    return path


def candidate(video: Path) -> QACandidate:
    return QACandidate(
        experiment_id="00000000-0000-0000-0000-000000000001",
        media_path=video,
        genes=FIRST_SHORT.genome.genes.model_dump(mode="json"),
        creative_spec=FIRST_SHORT.genome.creative_spec,
        hypothesis=FIRST_SHORT.hypothesis.statement,
        requirements=FIRST_SHORT.output.model_dump(mode="json"),
        ip_spec=dict(FIRST_SHORT.ip.spec),
    )


def run(
    video: Path, response: ContentReview | Exception, **thresholds: float
) -> dict[str, QAVerdict]:
    llm = FakeLanguageModel([response])
    reviewer = ContentReviewer(
        lambda request, schema, experiment_id: llm.generate(request, schema).parsed
    )
    gates = content_gates(reviewer, ContentThresholds(**thresholds))
    verdicts = {}
    for gate in gates:
        try:
            verdicts[gate.name] = gate.evaluate(candidate(video))
        except LLMError:
            verdicts[gate.name] = QAVerdict(
                gate=gate.name, gate_version=gate.version, outcome=QAOutcome.ESCALATE
            )
    assert len(llm.requests) <= 1  # one model call serves all four gates
    return verdicts


# --- frames ------------------------------------------------------------------


def test_frames_are_sampled_evenly_and_downscaled(video: Path, tmp_path: Path) -> None:
    frames = sample_frames(video, tmp_path, count=4, max_height=480)

    assert len(frames) == 4
    assert [round(f.at_seconds, 1) for f in frames] == [0.5, 1.5, 2.5, 3.5]
    assert all(f.path.exists() and f.path.suffix == ".jpg" for f in frames)
    assert all(f.path.stat().st_size < 200_000 for f in frames)


# --- the four gates ------------------------------------------------------------


def test_four_mandatory_content_gates_are_built(video: Path) -> None:
    gates: list[QAGate] = content_gates(ContentReviewer(lambda *a: review()), ContentThresholds())

    assert [(g.name, g.mandatory) for g in gates] == [
        ("child_safety", True),
        ("visual", True),
        ("ip_brand", True),
        ("creative", True),
    ]


def test_a_clean_video_passes_every_gate_with_one_model_call(video: Path) -> None:
    verdicts = run(video, review())

    assert {name: v.outcome for name, v in verdicts.items()} == {
        "child_safety": QAOutcome.PASS,
        "visual": QAOutcome.PASS,
        "ip_brand": QAOutcome.PASS,
        "creative": QAOutcome.PASS,
    }


def test_the_model_sees_frames_the_spec_and_the_ip_safety_rules(video: Path) -> None:
    seen: list[LLMRequest] = []

    def respond(request: LLMRequest) -> ContentReview:
        seen.append(request)
        return review()

    run(video, respond, frames=8)  # type: ignore[arg-type]

    [request] = seen
    assert request.purpose == "qa_content_review"
    assert len(request.images) == 8
    assert FIRST_SHORT.genome.creative_spec in request.prompt
    assert "No scary imagery" in request.prompt
    assert FIRST_SHORT.hypothesis.statement in request.prompt
    assert "cream fur" in request.prompt  # the character's reference appearance


@pytest.mark.parametrize("category", SAFETY_CATEGORIES)
def test_any_clear_safety_finding_fails_child_safety(video: Path, category: str) -> None:
    verdict = run(video, review(safety=safety(**{category: "clear"})))["child_safety"]

    assert verdict.outcome is QAOutcome.FAIL
    assert category in verdict.reasons[0]
    assert "seen at 0:05" in verdict.reasons[0]


def test_a_possible_safety_finding_escalates_to_a_human(video: Path) -> None:
    verdict = run(video, review(safety=safety(inappropriate_fear="possible")))["child_safety"]

    assert verdict.outcome is QAOutcome.ESCALATE
    assert "inappropriate_fear" in verdict.reasons[0]


def test_a_review_that_skips_a_safety_category_is_not_trusted(video: Path) -> None:
    incomplete = review(safety=safety()[:-1])

    verdict = run(video, incomplete)["child_safety"]

    assert verdict.outcome is QAOutcome.ESCALATE
    assert "not assessed" in verdict.reasons[0]


def test_engagement_potential_cannot_soften_a_safety_failure(video: Path) -> None:
    glowing = review(
        safety=safety(dangerous_imitation="clear"),
        creative={
            "story_clarity": 1.0, "age_appropriateness": 1.0, "pacing": 1.0,
            "ending_clarity": 1.0, "hypothesis_alignment": 1.0, "notes": "Will go viral.",
        },
    )  # fmt: skip

    assert run(video, glowing)["child_safety"].outcome is QAOutcome.FAIL


def test_visual_scores_below_the_threshold_fail_and_the_threshold_is_configurable(
    video: Path,
) -> None:
    drifting = review(
        visual={
            "character_consistency": 0.3, "continuity": 0.9, "visual_integrity": 0.9,
            "free_of_garbled_text": 1.0, "notes": "Nib's cape changes colour.",
        }
    )  # fmt: skip

    strict = run(video, drifting)["visual"]
    lenient = run(video, drifting, min_visual_score=0.2)["visual"]

    assert strict.outcome is QAOutcome.FAIL
    assert strict.scores["character_consistency"] == 0.3
    assert "character_consistency" in strict.reasons[0]
    assert lenient.outcome is QAOutcome.PASS


def test_brand_or_character_resemblance_is_flagged_for_review(video: Path) -> None:
    lookalike = review(
        ip={
            "logos_or_brands": False, "resembles_known_character": True,
            "resembles": "Sonic", "notes": "Blue spiky silhouette.",
        }
    )  # fmt: skip

    verdict = run(video, lookalike)["ip_brand"]

    assert verdict.outcome is QAOutcome.ESCALATE
    assert "Sonic" in verdict.reasons[0]


def test_a_visible_logo_is_flagged_for_review(video: Path) -> None:
    logo = review(
        ip={"logos_or_brands": True, "resembles_known_character": False, "resembles": "",
            "notes": "A swoosh on a shoe."}
    )  # fmt: skip

    assert run(video, logo)["ip_brand"].outcome is QAOutcome.ESCALATE


def test_creative_gate_reports_structured_scores(video: Path) -> None:
    verdict = run(video, review())["creative"]

    assert set(verdict.scores) == {
        "story_clarity", "age_appropriateness", "pacing", "ending_clarity", "hypothesis_alignment",
    }  # fmt: skip


def test_content_that_is_not_age_appropriate_fails_the_creative_gate(video: Path) -> None:
    too_old = review(
        creative={
            "story_clarity": 0.9, "age_appropriateness": 0.3, "pacing": 0.9,
            "ending_clarity": 0.9, "hypothesis_alignment": 0.9, "notes": "Sarcasm throughout.",
        }
    )  # fmt: skip

    verdict = run(video, too_old)["creative"]

    assert verdict.outcome is QAOutcome.FAIL
    assert "age_appropriateness" in verdict.reasons[0]


def test_a_video_that_does_not_embody_its_hypothesis_fails_the_creative_gate(video: Path) -> None:
    off_brief = review(
        creative={
            "story_clarity": 0.9, "age_appropriateness": 0.9, "pacing": 0.9,
            "ending_clarity": 0.9, "hypothesis_alignment": 0.1, "notes": "No visual question.",
        }
    )  # fmt: skip

    assert run(video, off_brief)["creative"].outcome is QAOutcome.FAIL


def test_when_the_model_is_unavailable_every_content_gate_raises(video: Path) -> None:
    verdicts = run(video, LLMError("overloaded", retryable=True))

    assert all(v.outcome is QAOutcome.ESCALATE for v in verdicts.values())
