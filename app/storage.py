"""Asset storage port. Binaries live in object storage; the database keeps
only metadata, hashes, and the storage URI."""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class AssetNotFound(Exception):
    """No object exists at the given storage URI."""


class AssetNotPublic(Exception):
    """The store has no public URL for its objects."""


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

    def public_url(self, uri: str) -> str:
        """A permanent, unauthenticated HTTPS URL for the object, as required
        by publishers that fetch media themselves. Raises ``AssetNotPublic``."""
        ...
