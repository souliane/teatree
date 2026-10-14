import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from teatree.utils.master_key import KeyFileSource, master_key_source
from teatree.utils.secrets import SecretStoreError
from tests.teatree_utils.test_sealing import KEY_TEXT


def test_source_defaults_beside_control_db(tmp_path: Path) -> None:
    source = master_key_source({"T3_CONTROL_DB_DIR": str(tmp_path)})
    assert source.label == f"file:{tmp_path / 'master.key'}"
    assert source.path == tmp_path / "master.key"


@pytest.mark.parametrize("source", ["gcp:secret", "file:relative/path", "invalid"])
def test_invalid_source_fails_without_fallback(source: str) -> None:
    with pytest.raises(ValueError, match="T3_MASTER_KEY_SOURCE"):
        master_key_source({"T3_MASTER_KEY_SOURCE": source})


def test_file_mode_is_private_and_key_text_is_not_reported(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    path.write_text(KEY_TEXT)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0644") as exc:
        KeyFileSource(path).load()
    assert str(path) in str(exc.value)
    assert KEY_TEXT not in str(exc.value)
    path.chmod(0o600)
    assert KeyFileSource(path).load().key_id


def test_read_only_private_key_is_loadable(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    path.write_text(KEY_TEXT)
    path.chmod(0o400)
    assert KeyFileSource(path).load().key_id


def test_create_under_restrictive_umask_can_be_reloaded(tmp_path: Path) -> None:
    source = KeyFileSource(tmp_path / "master.key")
    previous = os.umask(0o277)
    try:
        created = source.create()
    finally:
        os.umask(previous)
    assert source.path.stat().st_mode & 0o777 == 0o400
    assert source.load().key_id == created.key_id


def test_create_in_missing_directory_raises_store_error(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError, match="master key"):
        KeyFileSource(tmp_path / "missing" / "master.key").create()


def _create_key(path: str) -> str:
    return KeyFileSource(Path(path)).create().key_id


def test_eight_processes_share_one_key(tmp_path: Path) -> None:
    path = str(tmp_path / "master.key")
    with ProcessPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(_create_key, [path] * 8))
    assert len(set(ids)) == 1
    assert KeyFileSource(Path(path)).load().key_id == ids[0]


def test_concurrent_creators_keep_one_key(tmp_path: Path) -> None:
    source = KeyFileSource(tmp_path / "master.key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: source.create().key_id, range(2)))
    assert len(set(ids)) == 1
    assert source.path.stat().st_mode & 0o777 == 0o600
    assert source.load().key_id == ids[0]
