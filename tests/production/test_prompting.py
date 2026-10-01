from app.experiments.fixtures import FIRST_SHORT
from app.production.prompting import build_video_prompt


def test_prompt_carries_the_creative_spec_visual_identity_and_safety_constraints() -> None:
    prompt = build_video_prompt(
        ip_spec=dict(FIRST_SHORT.ip.spec),
        genes=FIRST_SHORT.genome.genes,
        creative_spec=FIRST_SHORT.genome.creative_spec,
    )

    assert FIRST_SHORT.genome.creative_spec in prompt
    assert "Soft 3D storybook look" in prompt
    assert "No scary imagery" in prompt
    assert "vertical 9:16" in prompt


def test_prompt_is_deterministic_for_the_same_inputs() -> None:
    kwargs = {
        "ip_spec": dict(FIRST_SHORT.ip.spec),
        "genes": FIRST_SHORT.genome.genes,
        "creative_spec": FIRST_SHORT.genome.creative_spec,
    }

    assert build_video_prompt(**kwargs) == build_video_prompt(**kwargs)  # type: ignore[arg-type]
