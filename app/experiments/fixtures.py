"""Hard-coded experiment for the vertical slice.

One safe, gentle concept for ages 4–8 that exercises the whole path
(spec → genome → generation → asset → lineage → review) without any creative
agent.

Production genes are chosen to fit the budget contract: Wan 3.0 at 480p with
native audio lists at $0.05/s, so one 8-second shot is about $0.40 (under the
$0.50 target). 720p would be $0.80 (see docs/research/higgsfield-api.md).
"""

from app.creative.genome import Genes, Genome
from app.creative.hypotheses import Metric, Prediction
from app.experiments.spec import ExperimentSpec, HypothesisSpec, IPSpec, OutputRequirements

FIRST_SHORT = ExperimentSpec(
    ip=IPSpec(
        slug="nibbin-hollow",
        name="Nibbin Hollow",
        category="narrative_adventure",
        spec={
            "age_range": [4, 8],
            "premise": (
                "Nib, a small round hedgehog-like creature in a leaf cape, explores a mossy "
                "forest hollow and solves tiny, gentle mysteries with patience and curiosity."
            ),
            "world_rules": [
                "The hollow is always safe: no villains, no chases, no peril.",
                "Every mystery has a kind, natural explanation.",
                "Nib never speaks; feelings show through gesture and soft sounds.",
            ],
            "visual_identity": (
                "Soft 3D storybook look, warm rim light, rounded shapes, moss greens and "
                "amber glows, shallow depth of field."
            ),
            "safety_constraints": [
                "No scary imagery, darkness is cosy not threatening.",
                "No dangerous behaviour a child could imitate.",
                "No text, logos, brands, or resemblance to existing characters.",
            ],
        },
    ),
    hypothesis=HypothesisSpec(
        statement=(
            "Opening on an unexplained gentle glow (a visual question) in the first two "
            "seconds raises completion rate versus a plain establishing shot."
        ),
        rationale=(
            "A curiosity gap gives a 4–8-year-old a reason to stay for the reveal; the "
            "reveal lands inside the same short so the question is always answered."
        ),
        prediction=Prediction(
            metric=Metric.COMPLETION_RATE,
            direction="increase",
            compared_to="ip_baseline",
            minimum_relative_effect=0.1,
            genes_under_test=("hook_type",),
        ),
        source="fixture",
    ),
    genome=Genome(
        genes=Genes(
            primary_character="Nib",
            supporting_characters=(),
            topic="where a mysterious glow in the hollow comes from",
            story_archetype="tiny_mystery",
            educational_goal="fireflies glow at dusk",
            hook_type="visual_question",
            dominant_emotion="wonder",
            visual_style="soft_3d_storybook",
            camera_style="slow_push_in",
            duration_seconds=8,
            scene_count=1,
            pace="gentle",
            narrator_style="none",
            dialogue_density="none",
            music_style="soft_music_box",
            ending_type="warm_reveal",
            cta_type="none",
            video_model="alibaba/wan-3.0/text-to-video",
            prompt_strategy="single_shot_v1",
            resolution="480p",
            aspect_ratio="9:16",
        ),
        creative_spec=(
            "Dusk in a mossy forest hollow. A soft amber glow pulses behind a fern. Nib, a "
            "small round hedgehog-like creature wearing a leaf cape, tilts their head, then "
            "tiptoes toward the light. Nib gently parts the fern: a cluster of fireflies is "
            "waking up, drifting upward like tiny lanterns. Nib's eyes widen with delight as "
            "the fireflies light the whole hollow. Calm, cosy, wondrous. No text on screen."
        ),
    ),
    output=OutputRequirements(
        aspect_ratio="9:16",
        min_duration_seconds=4,
        max_duration_seconds=15,
        min_height=800,  # 480p vertical output is roughly 480x832–854
        audio_expected=True,
    ),
    generation_reason=(
        "Vertical-slice fixture: hard-coded concept proving spec → generation → lineage → "
        "manual review end to end. Not proposed by a creative agent."
    ),
)
