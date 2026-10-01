"""Asset storage port. Binaries live in object storage; the database keeps
only metadata, hashes, and the storage URI."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class StoredObject(BaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str
    sha256: str
    size_bytes: int


class AssetStore(Protocol):
    def put_file(self, source: Path, key: str) -> StoredObject:
        """Store ``source`` under ``key`` and return its URI and content hash."""
        ...

    def local_path(self, uri: str) -> Path:
        """A local filesystem path holding the object's bytes (for probing,
        QA, and serving to a reviewer)."""
        ...
