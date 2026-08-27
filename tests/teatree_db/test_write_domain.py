"""The two facts `t3 doctor` now asserts about the control DB, each planted and observed.

The check these replace asked whether a claim FILE existed beside the database. It was
green through five corruptions in one day, because a marker written once says nothing
about the descriptors that were already open when it was written.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.db import write_domain
from teatree.db.write_domain import ControlDbWriteDomain, DescriptorTableUnavailableError, read_write_holders_across
from teatree.paths import CONTROL_DB_DIR_ENV
from teatree.utils.run import CommandFailedError


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


class TestReadWriteHoldersAcrossManyPaths:
    """One descriptor-table read answers for every path.

    Asking per path re-reads the whole table per path. A real data dir holds 52
    control-DB artifacts — the ``-wal``/``-shm`` sidecars plus every dated rename —
    so the per-path shape turned one probe into 52 and a doctor run into a
    20-second one.
    """

    def test_writers_on_several_files_are_all_reported_and_attributed(self, tmp_path: Path) -> None:
        held = [tmp_path / "db.sqlite3", tmp_path / "db.sqlite3-wal", tmp_path / "db.sqlite3.precorrupt-20260727"]
        unheld = tmp_path / "db.sqlite3-shm"
        for path in [*held, unheld]:
            path.write_bytes(b"x")

        with held[0].open("r+b"), held[1].open("r+b"), held[2].open("r+b"):
            found = read_write_holders_across([*held, unheld])

        assert {path for path, _ in found} == set(held)
        assert {holder.pid for _, holder in found} == {os.getpid()}

    def test_a_read_only_descriptor_is_not_a_writer(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(b"x")

        with database.open("rb"):
            assert read_write_holders_across([database]) == []

    def test_one_process_with_several_descriptors_on_one_file_is_reported_once(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(b"x")

        with database.open("r+b"), database.open("r+b"), database.open("r+b"):
            assert len(read_write_holders_across([database])) == 1

    def test_an_empty_path_set_reads_nothing_at_all(self, tmp_path: Path) -> None:
        assert read_write_holders_across([]) == []

    def test_the_descriptor_table_is_read_once_for_every_path(self, tmp_path: Path) -> None:
        # The whole point. Forced onto the lsof branch (the non-Linux view) because it
        # is the one whose cost is a subprocess, and the one a call count can observe.
        # `_which` is pinned too: a runner without lsof would otherwise never reach the
        # invocation this counts, and the count would read 0 for the wrong reason.
        paths = [tmp_path / f"db.sqlite3.copy-{index}" for index in range(12)]
        for path in paths:
            path.write_bytes(b"x")
        invocations: list[list[str]] = []

        def record(command: list[str], **_: object) -> object:
            invocations.append(command)
            raise CommandFailedError(command, 1, "", "")

        with (
            patch.object(write_domain, "_PROC", tmp_path / "no-procfs"),
            patch.object(write_domain, "_which", lambda _: "/usr/bin/lsof"),
            patch.object(write_domain, "run_allowed_to_fail", record),
            pytest.raises(DescriptorTableUnavailableError),
        ):
            read_write_holders_across(paths)

        assert len(invocations) == 1, f"one read must cover every path, got {len(invocations)}"
        assert {str(path) for path in paths} <= set(invocations[0])


class TestAnUnreadableTableIsNotAnAnswer:
    """Unmeasured must not reach a caller as measured-empty — that is what certifies a lie."""

    def test_no_descriptor_view_at_all_raises_rather_than_reporting_no_writers(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(b"x")

        with (
            patch.object(write_domain, "_PROC", tmp_path / "no-procfs"),
            patch.object(write_domain, "_which", lambda _: None),
            pytest.raises(DescriptorTableUnavailableError, match="no descriptor view"),
        ):
            read_write_holders_across([database])

    def test_a_failed_lsof_invocation_names_itself_rather_than_yielding_nothing(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(b"x")

        refusal = "lsof: permission denied"

        def explode(command: list[str], **_: object) -> object:
            raise OSError(refusal)

        with (
            patch.object(write_domain, "_PROC", tmp_path / "no-procfs"),
            patch.object(write_domain, "_which", lambda _: "/usr/bin/lsof"),
            patch.object(write_domain, "run_allowed_to_fail", explode),
            pytest.raises(DescriptorTableUnavailableError, match="permission denied"),
        ):
            read_write_holders_across([database])
