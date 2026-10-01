"""Turn a genome and creative spec into provider-neutral prompts.

Prompt construction is deterministic: the same genome always yields the same
prompt, so a stored genome plus the strategy name reproduces what was asked.
"""

from typing import Any

from app.creative.genome import Genes


def _words(token: str) -> str:
    return token.replace("_", " ")


def build_video_prompt(
    *,
    ip_spec: dict[str, Any],
    genes: Genes,
    creative_spec: str,
    duration_seconds: int | None = None,
    scene: tuple[int, int] | None = None,
) -> str:
    """Prompt strategy ``single_shot_v1``: one continuous shot per prompt.
    ``creative_spec`` is the whole story, or one scene of it when ``scene`` is
    ``(index, total)``."""
    seconds = duration_seconds or genes.duration_seconds
    lines = [creative_spec.strip(), ""]
    cast = {genes.primary_character, *genes.supporting_characters}
    for character in ip_spec.get("characters") or []:
        if character.get("name") in cast and character.get("appearance"):
            lines.append(f"Character: {character['appearance']}")
    if visual_identity := ip_spec.get("visual_identity"):
        lines.append(f"Visual identity: {visual_identity}")
    lines.append(
        f"Style: {_words(genes.visual_style)}; camera: {_words(genes.camera_style)}; "
        f"pace: {_words(genes.pace)}; mood: {_words(genes.dominant_emotion)}."
    )
    lines.append(
        f"Audio: {_words(genes.music_style)} music; narration: {_words(genes.narrator_style)}; "
        f"dialogue: {_words(genes.dialogue_density)}."
    )
    lines.append(
        f"Format: vertical {genes.aspect_ratio} short video, about {seconds} seconds, one "
        "continuous shot."
    )
    if scene is not None and scene[1] > 1:
        lines.append(
            f"This is scene {scene[0]} of {scene[1]} of one short video: keep the characters, "
            "setting and style identical across scenes."
        )
    lines.append("Audience: young children aged 4–8. Gentle, warm, and safe.")
    safety = ip_spec.get("safety_constraints") or []
    if safety:
        lines.append("Constraints: " + " ".join(str(item) for item in safety))
    return "\n".join(lines)


_POLICY_WORDS = (
    "nsfw",
    "policy",
    "moderation",
    "safety",
    "flagged",
    "inappropriate",
    "ip_detected",
)


def repair_prompt(prompt: str, problem: str | None) -> str:
    """Deterministic prompt repair after a failed or rejected attempt: keep the
    original prompt and append a simplifying revision note."""
    notes = ["Keep it very simple: one clear gentle action, steady camera, no on-screen text."]
    lowered = (problem or "").lower()
    if any(word in lowered for word in _POLICY_WORDS):
        notes.append("Fully family-friendly: soft, friendly, clearly non-threatening imagery only.")
    elif problem:
        notes.append(f"The previous render had this problem, avoid it: {problem}")
    return f"{prompt}\n\nRevision after a failed attempt: {' '.join(notes)}"
