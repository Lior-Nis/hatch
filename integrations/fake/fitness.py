"""Fake fitness evaluator returning a fixed score."""

from app.fitness.ports import FitnessInputs, FitnessResult


class FakeFitnessEvaluator:
    name = "fake"
    version = "fake-1"

    def __init__(self, *, score: float) -> None:
        self._score = score

    def evaluate(self, inputs: FitnessInputs) -> FitnessResult:
        return FitnessResult(
            evaluator=self.name,
            evaluator_version=self.version,
            score=self._score,
            inputs=inputs,
        )
