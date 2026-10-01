"""Storyboard: the scene plan for one video.

A single-scene video needs no planning: the creative spec is the scene. For
several scenes a language model splits the creative spec, and Hatch checks the
result against the genome (scene count, total duration) before spending
anything on generation.
"""

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.budgets.governor import BudgetGovernor
from app.creative.genome import Genes
from app.experiments.models import Experiment
from app.llm.calls import call_model
from app.llm.ports import LanguageModel, LLMRequest
from app.production.ports import ProductionError


class Scene(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int = Field(ge=1)
    description: str = Field(min_length=1)
    duration_seconds: int = Field(gt=0)


class Storyboard(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenes: list[Scene]


_SYSTEM = """\
You plan the shots of short vertical videos for children aged 4 to 8. Split the creative \
specification into the requested number of scenes. Each scene is one continuous shot that a \
video model can render on its own, so describe the characters and setting again in every \
scene, in concrete visual terms. Keep everything gentle and safe. Scene durations are whole \
seconds and must add up exactly to the total duration."""


def build_storyboard(
    session: Session,
    experiment: Experiment,
    genes: Genes,
    *,
    llm: LanguageModel | None,
    governor: BudgetGovernor,
) -> tuple[Storyboard, str]:
    """Returns the storyboard and where it came from (``genome`` or ``language_model``)."""
    if genes.scene_count == 1:
        scene = Scene(
            index=1,
            description=experiment.genome.creative_spec,
            duration_seconds=genes.duration_seconds,
        )
        return Storyboard(scenes=[scene]), "genome"
    if llm is None:
        raise ProductionError(
            f"a storyboard for {genes.scene_count} scenes needs a language model "
            "(set HATCH_ANTHROPIC_API_KEY) — or use a single-scene genome"
        )
    request = LLMRequest(
        purpose="storyboard",
        system=_SYSTEM,
        prompt=(
            f"Creative specification:\n{experiment.genome.creative_spec}\n\n"
            f"Number of scenes: {genes.scene_count}\n"
            f"Total duration: {genes.duration_seconds} seconds\n"
            f"Pace: {genes.pace}. Camera: {genes.camera_style}."
        ),
        max_output_tokens=4000,
    )
    board = call_model(
        session, llm, governor, request, Storyboard, experiment_id=experiment.id
    ).parsed
    indexes = [scene.index for scene in board.scenes]
    total = sum(scene.duration_seconds for scene in board.scenes)
    if indexes != list(range(1, genes.scene_count + 1)):
        raise ProductionError(
            f"storyboard has {len(board.scenes)} scenes, but the genome asks for "
            f"{genes.scene_count} scenes numbered from 1"
        )
    if total != genes.duration_seconds:
        raise ProductionError(
            f"storyboard scenes last {total}s in total, but the genome asks for "
            f"{genes.duration_seconds}s"
        )
    return board, "language_model"
