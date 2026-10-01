"""S3-compatible asset store, exercised over HTTP against a local S3 server
(moto), so the real boto3 request path is used."""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer
from sqlalchemy.orm import Session

from app.storage import AssetNotFound
from integrations.object_storage.s3 import S3AssetStore
from tests.factories import final_asset, make_generated_experiment

BUCKET = "hatch-test-assets"


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def store(s3_endpoint: str, tmp_path: Path) -> Iterator[S3AssetStore]:
    store = S3AssetStore.connect(
        bucket=BUCKET,
        endpoint_url=s3_endpoint,
        access_key_id="test",
        secret_access_key="test",
        region="us-east-1",
        cache_dir=tmp_path / "cache",
    )
    client = boto3.client(
        "s3", endpoint_url=s3_endpoint, aws_access_key_id="test",
        aws_secret_access_key="test", region_name="us-east-1",
    )  # fmt: skip
    client.create_bucket(Bucket=BUCKET)
    yield store
    for item in client.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        client.delete_object(Bucket=BUCKET, Key=item["Key"])
    client.delete_bucket(Bucket=BUCKET)
    client.close()
    store.close()


def test_uploaded_file_can_be_downloaded_with_identical_bytes(
    store: S3AssetStore, tmp_path: Path
) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"video-bytes" * 1000)

    stored = store.put_file(source, "experiments/e1/a1.mp4")
    source.unlink()

    assert stored.uri == f"s3://{BUCKET}/experiments/e1/a1.mp4"
    assert stored.size_bytes == 11_000
    assert stored.sha256 == hashlib.sha256(b"video-bytes" * 1000).hexdigest()
    assert store.local_path(stored.uri).read_bytes() == b"video-bytes" * 1000


def test_download_is_cached_locally(store: S3AssetStore, tmp_path: Path) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"abc")
    stored = store.put_file(source, "k/one.bin")

    first = store.local_path(stored.uri)
    first.write_bytes(b"sentinel")  # a second call must not download again

    assert store.local_path(stored.uri).read_bytes() == b"sentinel"


def test_missing_object_raises_asset_not_found(store: S3AssetStore) -> None:
    with pytest.raises(AssetNotFound):
        store.local_path(f"s3://{BUCKET}/nope.mp4")


def test_uri_for_another_bucket_or_scheme_is_rejected(store: S3AssetStore) -> None:
    for uri in ("s3://someone-elses-bucket/x.mp4", "local://x.mp4"):
        with pytest.raises(ValueError, match="not an asset"):
            store.local_path(uri)


def test_generated_video_bytes_live_in_object_storage_not_in_postgres(
    session: Session, store: S3AssetStore, tmp_path: Path
) -> None:
    experiment = make_generated_experiment(session, tmp_path, store=store)

    asset = final_asset(experiment)

    assert asset.storage_uri.startswith(f"s3://{BUCKET}/experiments/{experiment.id}/")
    downloaded = store.local_path(asset.storage_uri)
    assert downloaded.stat().st_size == asset.size_bytes
    assert hashlib.sha256(downloaded.read_bytes()).hexdigest() == asset.sha256
