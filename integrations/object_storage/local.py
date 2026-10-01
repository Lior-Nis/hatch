"""Filesystem-backed asset store for local development and tests."""

import hashlib
import shutil
from pathlib import Path

from app.storage import AssetNotPublic, StoredObject

_SCHEME = "local://"


class LocalAssetStore:
    def __init__(self, root: Path, *, public_base_url: str | None = None) -> None:
        self._root = root.resolve()
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def public_url(self, uri: str) -> str:
        if self._public_base_url is None:
            raise AssetNotPublic(
                "local asset storage is not reachable from the internet; configure S3/R2 "
                "storage and HATCH_ASSET_PUBLIC_BASE_URL to publish"
            )
        self._resolve(uri.removeprefix(_SCHEME))  # validates the key
        return f"{self._public_base_url}/{uri.removeprefix(_SCHEME)}"

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
