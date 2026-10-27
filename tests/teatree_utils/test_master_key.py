import errno
import logging
import multiprocessing
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import QueueHandler
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.master_key import KeyFileSource, MasterKeySourceError, master_key_source
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


def test_link_refusal_raises_store_error_and_leaves_no_file(tmp_path: Path) -> None:
    directory = tmp_path / "keys"
    directory.mkdir()
    with (
        patch("teatree.utils.master_key.os.link", side_effect=PermissionError("hard links refused")),
        pytest.raises(MasterKeySourceError, match="could not be created"),
    ):
        KeyFileSource(directory / "master.key").create()
    assert list(directory.iterdir()) == []


def test_read_only_key_directory_points_at_the_readme(tmp_path: Path) -> None:
    real_open = os.open

    def read_only(path: Path, flags: int, *args: int) -> int:
        if flags & os.O_CREAT:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, *args)

    with (
        patch("teatree.utils.master_key.os.open", side_effect=read_only),
        pytest.raises(MasterKeySourceError, match=r"read-only.*deploy/README.md"),
    ):
        KeyFileSource(tmp_path / "master.key").create()


def test_directory_sync_failure_raises_store_error(tmp_path: Path) -> None:
    directory = tmp_path / "keys"
    directory.mkdir()
    real_open = os.open

    def refuse_directory(path: Path, flags: int, *args: int) -> int:
        if Path(path) == directory:
            message = "directory not openable"
            raise PermissionError(message)
        return real_open(path, flags, *args)

    with (
        patch("teatree.utils.master_key.os.open", side_effect=refuse_directory),
        pytest.raises(MasterKeySourceError, match="directory could not be synced"),
    ):
        KeyFileSource(directory / "master.key").create()
    assert [path.name for path in directory.iterdir()] == ["master.key"]


def _production_env(volume: Path, **extra: str) -> dict[str, str]:
    return {"T3_CONTROL_DB_DIR": str(volume), "TEATREE_ROLE": "worker", **extra}


def test_production_default_inside_the_volume_is_refused_for_create(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    with pytest.raises(MasterKeySourceError, match="control-database volume"):
        master_key_source(_production_env(volume)).create()
    assert list(volume.iterdir()) == []


def test_production_explicit_path_inside_the_volume_through_a_symlink_is_refused(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    (tmp_path / "alias").symlink_to(volume)
    environment = _production_env(
        volume, T3_MASTER_KEY_SOURCE=f"file:{tmp_path / 'alias' / 'sub' / '..' / 'master.key'}"
    )
    with pytest.raises(MasterKeySourceError, match="control-database volume"):
        master_key_source(environment).create()
    assert list(volume.iterdir()) == []


def test_production_path_outside_the_volume_creates_the_key(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    keys = tmp_path / "keys"
    volume.mkdir()
    keys.mkdir()
    source = master_key_source(_production_env(volume, T3_MASTER_KEY_SOURCE=f"file:{keys / 'master.key'}"))
    assert source.create().key_id == source.load().key_id


def test_unset_role_keeps_the_default_inside_the_volume(tmp_path: Path) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    source = master_key_source({"T3_CONTROL_DB_DIR": str(volume)})
    assert source.create().key_id
    assert (volume / "master.key").exists()


def test_production_load_inside_the_volume_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    (volume / "master.key").write_text(KEY_TEXT)
    (volume / "master.key").chmod(0o600)
    source = master_key_source(_production_env(volume))
    with caplog.at_level("WARNING", logger="teatree.utils.master_key"):
        source.load()
        source.load()
    warnings = [record.getMessage() for record in caplog.records if "control-database volume" in record.getMessage()]
    expected = (
        f"master key {source.label} sits inside the control-database volume; "
        "a copy of the volume carries the key with the rows it seals (deploy/README.md)"
    )
    assert warnings == [expected]


def test_explicit_missing_source_never_opens_the_default_key(tmp_path: Path) -> None:
    default = tmp_path / "control"
    default.mkdir()
    (default / "master.key").write_text(KEY_TEXT)
    (default / "master.key").chmod(0o600)
    explicit = tmp_path / "elsewhere" / "master.key"
    environment = {"T3_CONTROL_DB_DIR": str(default), "T3_MASTER_KEY_SOURCE": f"file:{explicit}"}
    with patch("teatree.utils.master_key.os.open", wraps=os.open) as spy, pytest.raises(SecretStoreError) as exc:
        master_key_source(environment).load()
    assert str(explicit) in str(exc.value)
    assert [call.args[0] for call in spy.call_args_list] == [explicit]


def test_in_volume_warning_is_once_across_new_source_instances(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    volume = tmp_path / "control"
    volume.mkdir()
    path = volume / "master.key"
    KeyFileSource(path).create()
    with caplog.at_level("WARNING", logger="teatree.utils.master_key"):
        KeyFileSource(path, volume=volume).load()
        KeyFileSource(path, volume=volume).load()
    assert sum("sits inside the control-database volume" in record.message for record in caplog.records) == 1


def _race_create(path: str, barrier: Barrier, results: Queue, creations: Queue) -> None:
    logging.getLogger("teatree.utils.master_key").addHandler(QueueHandler(creations))
    barrier.wait(timeout=30)
    results.put(KeyFileSource(Path(path)).create().key_id)


def test_eight_racing_processes_leave_one_key_and_no_temporary_file(tmp_path: Path) -> None:
    directory = tmp_path / "keys"
    directory.mkdir()
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(8)
    results = context.Queue()
    creations = context.Queue()
    workers = [
        context.Process(target=_race_create, args=(str(directory / "master.key"), barrier, results, creations))
        for _ in range(8)
    ]
    for worker in workers:
        worker.start()
    ids = {results.get(timeout=60) for _ in workers}
    for worker in workers:
        worker.join(timeout=60)
    assert [worker.exitcode for worker in workers] == [0] * 8
    assert len(ids) == 1
    assert creations.get(timeout=5).getMessage().endswith("keep a copy of this key")
    with pytest.raises(queue.Empty):
        creations.get(timeout=1)
    assert [path.name for path in directory.iterdir()] == ["master.key"]
    assert KeyFileSource(directory / "master.key").load().key_id in ids


def test_first_creation_warns_once_with_key_id_and_never_the_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = KeyFileSource(tmp_path / "master.key")
    with caplog.at_level("WARNING", logger="teatree.utils.master_key"):
        created = source.create()
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert created.key_id in record.getMessage()
    assert "keep a copy of this key" in record.getMessage()
    assert source.path.read_text() not in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="teatree.utils.master_key"):
        assert source.create().key_id == created.key_id
    assert caplog.records == []


def test_concurrent_creators_keep_one_key(tmp_path: Path) -> None:
    source = KeyFileSource(tmp_path / "master.key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: source.create().key_id, range(2)))
    assert len(set(ids)) == 1
    assert source.path.stat().st_mode & 0o777 == 0o600
    assert source.load().key_id == ids[0]
