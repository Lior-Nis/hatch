"""S3-compatible asset store (Cloudflare R2, AWS S3, any S3 API).

Objects are addressed as ``s3://<bucket>/<key>``. ``local_path`` downloads an
object once into a cache directory so tools that need a file (ffprobe, QA, the
review UI) can read it.
"""

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.storage import AssetNotFound, AssetNotPublic, StoredObject


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class S3AssetStore:
    def __init__(
        self, *, bucket: str, client: Any, cache_dir: Path, public_base_url: str | None = None
    ) -> None:
        self._public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self._bucket = bucket
        self._client = client
        self._cache_dir = cache_dir.resolve()
        self._prefix = f"s3://{bucket}/"

    @classmethod
    def connect(
        cls,
        *,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        cache_dir: Path,
        endpoint_url: str | None = None,
        region: str = "auto",
        public_base_url: str | None = None,
    ) -> "S3AssetStore":
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )
        return cls(
            bucket=bucket, client=client, cache_dir=cache_dir, public_base_url=public_base_url
        )

    def close(self) -> None:
        self._client.close()

    def put_file(self, source: Path, key: str) -> StoredObject:
        sha256 = _sha256(source)
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self._client.upload_file(
            str(source),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": sha256}},
        )
        return StoredObject(
            uri=f"{self._prefix}{key}", sha256=sha256, size_bytes=source.stat().st_size
        )

    def public_url(self, uri: str) -> str:
        if self._public_base_url is None:
            raise AssetNotPublic(
                "no public URL is configured for the bucket; set HATCH_ASSET_PUBLIC_BASE_URL"
            )
        if not uri.startswith(self._prefix):
            raise ValueError(f"not an asset in bucket {self._bucket}: {uri}")
        return f"{self._public_base_url}/{uri.removeprefix(self._prefix)}"

    def local_path(self, uri: str) -> Path:
        if not uri.startswith(self._prefix):
            raise ValueError(f"not an asset in bucket {self._bucket}: {uri}")
        key = uri.removeprefix(self._prefix)
        cached = (self._cache_dir / key).resolve()
        if not cached.is_relative_to(self._cache_dir):
            raise ValueError(f"not an asset key: {key}")
        if cached.exists():
            return cached
        cached.parent.mkdir(parents=True, exist_ok=True)
        partial = cached.with_name(f"{cached.name}.{os.getpid()}.part")
        try:
            self._client.download_file(self._bucket, key, str(partial))
        except ClientError as exc:
            partial.unlink(missing_ok=True)
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise AssetNotFound(uri) from exc
            raise
        partial.replace(cached)
        return cached
