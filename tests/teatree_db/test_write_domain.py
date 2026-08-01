"""The two facts `t3 doctor` now asserts about the control DB, each planted and observed.

The check these replace asked whether a claim FILE existed beside the database. It was
green through five corruptions in one day, because a marker written once says nothing
about the descriptors that were already open when it was written.
"""

import os
from pathlib import Path

import pytest

from teatree.db.write_domain import ControlDbWriteDomain
from teatree.paths import CONTROL_DB_DIR_ENV


class TestDatabaseIsOffTheHostFilesystem:
    def test_a_database_in_the_control_volume_is_isolated(self, tmp_path: Path) -> None:
        volume = tmp_path / "control-db"
        domain = ControlDbWriteDomain(volume / "db.sqlite3", env={CONTROL_DB_DIR_ENV: str(volume)})

        assert not domain.on_host_filesystem

    def test_a_database_in_the_bind_mounted_data_dir_is_not(self, tmp_path: Path) -> None:
        volume = tmp_path / "control-db"
        data_dir = tmp_path / "data"
        domain = ControlDbWriteDomain(data_dir / "db.sqlite3", env={CONTROL_DB_DIR_ENV: str(volume)})

        assert domain.on_host_filesystem, "the pre-move layout is exactly what this must catch"

    def test_the_expected_directory_comes_from_the_environment(self, tmp_path: Path) -> None:
        domain = ControlDbWriteDomain(tmp_path / "db.sqlite3", env={CONTROL_DB_DIR_ENV: "/var/lib/teatree/control-db"})

        assert domain.expected_dir == Path("/var/lib/teatree/control-db")


class TestReadWriteHolderDetection:
    """A real descriptor on a real file — the condition, not a proxy for it."""

    def test_a_live_read_write_descriptor_is_reported(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        domain = ControlDbWriteDomain(db, env={}, containerized=False)
        assert not domain.read_write_holders(), "control: nothing holds the file before it is opened"

        with db.open("r+b"):
            writers = domain.read_write_holders()

        assert [holder.pid for holder in writers] == [os.getpid()]
        assert writers[0].writable

    def test_a_read_only_descriptor_is_not_a_writer(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        domain = ControlDbWriteDomain(db, env={}, containerized=False)

        with db.open("rb"):
            assert domain.read_write_holders() == []

    def test_inside_the_container_a_read_write_holder_is_the_owning_stack(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        domain = ControlDbWriteDomain(db, env={}, containerized=True)

        with db.open("r+b"):
            assert domain.read_write_holders(), "control: the holder IS visible"
            assert domain.foreign_writers() == [], "a container-side writer is the legitimate owner"

    def test_outside_the_container_a_read_write_holder_is_foreign(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        domain = ControlDbWriteDomain(db, env={}, containerized=False)

        with db.open("r+b"):
            assert [str(writer) for writer in domain.foreign_writers()] != []

    def test_one_process_with_several_descriptors_is_reported_once(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite3"
        db.touch()
        domain = ControlDbWriteDomain(db, env={}, containerized=False)

        with db.open("r+b"), db.open("r+b"), db.open("r+b"):
            assert len(domain.read_write_holders()) == 1


@pytest.mark.parametrize("mode", ["r+b", "wb"])
def test_every_writable_open_mode_counts_as_a_writer(tmp_path: Path, mode: str) -> None:
    db = tmp_path / "db.sqlite3"
    db.touch()
    domain = ControlDbWriteDomain(db, env={}, containerized=False)

    with db.open(mode):
        assert domain.read_write_holders()
