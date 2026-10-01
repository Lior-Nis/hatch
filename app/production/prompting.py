"""Turn a genome and creative spec into provider-neutral prompts.

Prompt construction is deterministic: the same genome always yields the same
prompt, so a stored genome plus the strategy name reproduces what was asked.
"""

from typing import Any

from app.creative.genome import Genes


def _words(token: str) -> str:
    return token.replace("_", " ")


def build_video_prompt(*, ip_spec: dict[str, Any], genes: Genes, creative_spec: str) -> str:
    """Prompt strategy ``single_shot_v1``: one continuous shot, whole story."""
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
        f"Format: vertical {genes.aspect_ratio} short video, about {genes.duration_seconds} "
        f"seconds, {genes.scene_count} continuous scene(s)."
    )
    lines.append("Audience: young children aged 4–8. Gentle, warm, and safe.")
    safety = ip_spec.get("safety_constraints") or []
    if safety:
        lines.append("Constraints: " + " ".join(str(item) for item in safety))
    return "\n".join(lines)
