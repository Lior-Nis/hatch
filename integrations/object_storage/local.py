"""Filesystem-backed asset store for local development and tests."""

import hashlib
import shutil
from pathlib import Path

from app.storage import StoredObject

_SCHEME = "local://"


class LocalAssetStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def put_file(self, source: Path, key: str) -> StoredObject:
        destination = self._resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return StoredObject(
            uri=f"{_SCHEME}{key}", sha256=digest.hexdigest(), size_bytes=destination.stat().st_size
        )

    def local_path(self, uri: str) -> Path:
        if not uri.startswith(_SCHEME):
            raise ValueError(f"not a local asset URI: {uri}")
        return self._resolve(uri.removeprefix(_SCHEME))

    def _resolve(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"asset key escapes the store root: {key}")
        return path
