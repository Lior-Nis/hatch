"""Fake QA gate returning a fixed verdict."""

from app.quality.ports import QACandidate, QAOutcome, QAVerdict


class FakeQAGate:
    version = "fake-1"

    def __init__(
        self,
        *,
        name: str,
        mandatory: bool,
        outcome: QAOutcome = QAOutcome.PASS,
        reasons: list[str] | None = None,
    ) -> None:
        self.name = name
        self.mandatory = mandatory
        self._outcome = outcome
        self._reasons = tuple(reasons or [])

    def evaluate(self, candidate: QACandidate) -> QAVerdict:
        return QAVerdict(
            gate=self.name,
            gate_version=self.version,
            outcome=self._outcome,
            reasons=self._reasons,
        )
