"""Local store for platform OAuth tokens.

Tokens are credentials: they never go into the database, Todoist, or logs.
They live in one JSON file readable only by its owner (``var/credentials.json``
by default, which is git-ignored), keyed by ``<platform>:<account id>``.
"""

import json
import os
from pathlib import Path
from typing import Any


class TokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._read().get(key)
        return dict(entry) if isinstance(entry, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        data = self._read()
        data[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        # Create with owner-only permissions before any secret is written.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, self._path)
        os.chmod(self._path, 0o600)

    def keys(self) -> list[str]:
        return sorted(self._read())

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
