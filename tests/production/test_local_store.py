import hashlib
from pathlib import Path

from integrations.object_storage.local import LocalAssetStore


def test_put_file_stores_bytes_outside_the_database_and_returns_hash(tmp_path: Path) -> None:
    source = tmp_path / "in.bin"
    source.write_bytes(b"hello hatch")
    store = LocalAssetStore(tmp_path / "assets")

    stored = store.put_file(source, "experiments/e1/a1.mp4")

    assert stored.uri == "local://experiments/e1/a1.mp4"
    assert stored.size_bytes == 11
    assert stored.sha256 == hashlib.sha256(b"hello hatch").hexdigest()
    assert store.local_path(stored.uri).read_bytes() == b"hello hatch"


def test_local_path_refuses_to_escape_the_store_root(tmp_path: Path) -> None:
    store = LocalAssetStore(tmp_path / "assets")

    try:
        store.local_path("local://../../etc/passwd")
    except ValueError:
        return
    raise AssertionError("expected a ValueError for a path outside the store")
