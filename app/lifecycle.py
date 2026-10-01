"""Finite state machines for entity lifecycles.

A lifecycle is a table of legal transitions. Models validate every status
assignment against their lifecycle, so an illegal transition cannot be
persisted by any code path — not even by mistake.
"""

from collections.abc import Mapping
from enum import StrEnum


class IllegalTransition(Exception):
    def __init__(self, entity: str, current: StrEnum, target: StrEnum) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"illegal {entity} transition: {current.value} → {target.value}")


class Lifecycle[S: StrEnum]:
    def __init__(self, entity: str, *, initial: S, transitions: Mapping[S, set[S]]) -> None:
        self.entity = entity
        self.initial = initial
        self._transitions = {state: frozenset(targets) for state, targets in transitions.items()}

    @property
    def states(self) -> frozenset[S]:
        return frozenset(self._transitions)

    def allowed(self, current: S) -> frozenset[S]:
        return self._transitions[current]

    def can(self, current: S, target: S) -> bool:
        return target in self._transitions[current]

    def check(self, current: S, target: S) -> None:
        if not self.can(current, target):
            raise IllegalTransition(self.entity, current, target)

    def validate_assignment(self, current: S | None, target: S) -> S:
        """For model validators: ``current`` is None on a not-yet-persisted
        entity, which is treated as being in the initial state. Re-assigning
        the current state is a no-op."""
        origin = current if current is not None else self.initial
        if target != origin:
            self.check(origin, target)
        return target
