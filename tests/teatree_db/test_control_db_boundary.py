"""The control DB may be written from exactly one coherence domain.

WAL coordinates writers through an mmap'd ``-shm`` file whose coherence Docker
Desktop's shared-folder layer does not guarantee, so a host writer and a
containerized writer on the same bind-mounted file can allocate the same page —
which is what cross-linked ``teatree_outbound_claim`` pages in this install.

These tests drive the guard through REAL SQLite connections (the stdlib cold
writer and a real Django ``ConnectionHandler`` on the guarded engine), because a
mocked connection would pass while the downgrade silently stopped happening. The
container side is simulated the way production detects it — ``TEATREE_ROLE`` in
the environment — not by patching the detector.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.utils import ConnectionHandler
from pytest_django.plugin import DjangoDbBlocker

from teatree.config import cold_writer
from teatree.db.boundary import CLAIM_FILENAME, ControlDbBoundary, DbBoundaryError, control_db_unreachable_reason
from teatree.paths import CONTROL_DB_DIR_ENV
from teatree.settings import SQLITE_BOUNDARY_ENGINE, SQLITE_WRITE_SERIALIZATION_OPTIONS


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the test as a host process — the domain production detects by an absent role."""
    monkeypatch.delenv("TEATREE_ROLE", raising=False)


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the test as the containerized runtime — the deploy entrypoint sets this for every role."""
    monkeypatch.setenv("TEATREE_ROLE", "worker")


def _config_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, key TEXT, value TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(scope, key))"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _claimed_config_db(directory: Path) -> Path:
    db = _config_db(directory / "db.sqlite3")
    ControlDbBoundary(db, containerized=True).claim_for_container()
    return db


@contextmanager
def _connection(db: Path, blocker: DjangoDbBlocker) -> Iterator[BaseDatabaseWrapper]:
    """A real Django connection on the guarded engine, outside pytest-django's blocker."""
    handler = ConnectionHandler(
        {"default": {"ENGINE": SQLITE_BOUNDARY_ENGINE, "NAME": str(db), "OPTIONS": SQLITE_WRITE_SERIALIZATION_OPTIONS}}
    )
    try:
        with blocker.unblock():
            yield handler["default"]
    finally:
        handler.close_all()


def _row_count(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute("SELECT count(*) FROM teatree_config_setting").fetchone()[0])
    finally:
        conn.close()


class TestOwnership:
    """Who may write, and how ownership is acquired."""

    def test_an_unclaimed_database_is_writable_from_either_domain(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")

        assert ControlDbBoundary(db, containerized=False).read_write_allowed is True
        assert ControlDbBoundary(db, containerized=True).read_write_allowed is True

    def test_a_containerized_connection_claims_the_database(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")

        ControlDbBoundary(db, containerized=True).claim_for_container()

        assert (tmp_path / CLAIM_FILENAME).is_file()
        assert ControlDbBoundary(db, containerized=False).read_write_allowed is False

    def test_a_host_process_never_claims_the_database(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")

        ControlDbBoundary(db, containerized=False).claim_for_container()

        assert not (tmp_path / CLAIM_FILENAME).exists()

    def test_the_owning_container_keeps_write_access(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")
        ControlDbBoundary(db, containerized=True).claim_for_container()

        assert ControlDbBoundary(db, containerized=True).read_write_allowed is True

    def test_a_claim_beside_one_database_does_not_bind_another(self, tmp_path: Path) -> None:
        """A per-worktree isolated copy lives in its own directory and stays writable."""
        ControlDbBoundary(_config_db(tmp_path / "db.sqlite3"), containerized=True).claim_for_container()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        isolated = _config_db(worktree / "db.sqlite3")

        assert ControlDbBoundary(isolated, containerized=False).read_write_allowed is True

    @pytest.mark.parametrize("name", [":memory:", "", "file:memorydb_default?mode=memory&cache=shared"])
    def test_non_file_databases_are_out_of_scope(self, name: str) -> None:
        """Every test run uses one of these; the rule must not reach them."""
        boundary = ControlDbBoundary(name, containerized=False)

        assert boundary.file_backed is False
        assert boundary.read_write_allowed is True


class TestTheGuardedDjangoBackend:
    """The enforcement seam: reads survive the downgrade, writes fail loud."""

    _INSERT = (
        "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
        "VALUES ('', 'k', '1', 'now', 'now')"
    )

    def test_a_host_connection_to_a_claimed_database_can_still_read(
        self, tmp_path: Path, host: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        db = _claimed_config_db(tmp_path)

        with _connection(db, django_db_blocker) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM teatree_config_setting")

                row = cursor.fetchone()
                assert row is not None
                assert row[0] == 0
            assert connection.boundary_read_only is True

    def test_a_host_write_to_a_claimed_database_raises_the_boundary_error(
        self, tmp_path: Path, host: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        db = _claimed_config_db(tmp_path)

        with (
            _connection(db, django_db_blocker) as connection,
            pytest.raises(DbBoundaryError, match="deploy/t3"),
            connection.cursor() as cursor,
        ):
            cursor.execute(self._INSERT)

        assert _row_count(db) == 0, "the refused write must not have landed"

    def test_a_host_executemany_to_a_claimed_database_raises_the_boundary_error(
        self, tmp_path: Path, host: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        """``executemany`` is the second write verb, and it has its own translation arm."""
        db = _claimed_config_db(tmp_path)
        rows = [("", "a", "1", "now", "now"), ("", "b", "2", "now", "now")]

        with (
            _connection(db, django_db_blocker) as connection,
            pytest.raises(DbBoundaryError, match="deploy/t3"),
            connection.cursor() as cursor,
        ):
            cursor.executemany(
                "INSERT INTO teatree_config_setting (scope, key, value, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )

        assert _row_count(db) == 0, "the refused write must not have landed"

    def test_a_downgraded_cursor_still_serves_the_whole_dbapi_surface(
        self, tmp_path: Path, host: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        """The read path reaches the cursor by duck typing, not by ``sqlite3.Cursor`` identity.

        ``create_cursor`` hands back a delegate rather than a ``Cursor`` subclass,
        so the attributes and iteration Django's ``CursorWrapper`` forwards
        (``description``, ``fetchall``, ``yield from``) have to keep working
        through it.
        """
        db = _claimed_config_db(tmp_path)

        with _connection(db, django_db_blocker) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS one UNION ALL SELECT 2")

            description = cursor.description
            assert description is not None
            assert [column[0] for column in description] == ["one"]
            assert [row[0] for row in cursor] == [1, 2]

    def test_a_containerized_connection_writes_and_claims(
        self, tmp_path: Path, container: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        db = _config_db(tmp_path / "db.sqlite3")

        with _connection(db, django_db_blocker) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self._INSERT)
            connection.connection.commit()

            assert connection.boundary_read_only is False

        assert (tmp_path / CLAIM_FILENAME).is_file()
        assert _row_count(db) == 1

    def test_an_unclaimed_database_is_written_normally_from_the_host(
        self, tmp_path: Path, host: None, django_db_blocker: DjangoDbBlocker
    ) -> None:
        """The no-Docker install: nothing has ever claimed the file, so nothing changes."""
        db = _config_db(tmp_path / "db.sqlite3")

        with _connection(db, django_db_blocker) as connection:
            with connection.cursor() as cursor:
                cursor.execute(self._INSERT)
            connection.connection.commit()

        assert _row_count(db) == 1
        assert not (tmp_path / CLAIM_FILENAME).exists()


class TestTheDjangoFreeColdWriter:
    """The one canonical-DB writer the Django backend cannot cover."""

    def test_it_refuses_a_claimed_database_from_the_host(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")
        ControlDbBoundary(db, containerized=True).claim_for_container()

        with pytest.raises(DbBoundaryError, match="container"):
            cold_writer.write_setting("mode", "auto", db_path=db)

    def test_it_writes_an_unclaimed_database(self, tmp_path: Path) -> None:
        db = _config_db(tmp_path / "db.sqlite3")

        assert cold_writer.write_setting("mode", "auto", db_path=db) is cold_writer.WriteResult.WROTE


class TestControlDbUnreachableReason:
    """The topology predicate both the eval lane and ``ensure-pr`` consume.

    One question — "can a process aimed at this database reach it?" — asked before
    any DB work rather than inferred from an ``OperationalError`` afterwards. The
    canonical control DB directory is a container-only mount by design, so on the
    host the honest answer is a reason, and every caller that needs the ORM can then
    report itself as not-run instead of crashing.
    """

    _CONTAINER_ONLY = Path("/nonexistent/container-only/control-db")

    def _env(self, directory: Path) -> dict[str, str]:
        return {CONTROL_DB_DIR_ENV: str(directory)}

    def test_names_the_absent_container_only_directory(self) -> None:
        reason = control_db_unreachable_reason(self._CONTAINER_ONLY / "db.sqlite3", env=self._env(self._CONTAINER_ONLY))
        assert reason is not None
        assert str(self._CONTAINER_ONLY) in reason
        assert "container-only" in reason

    def test_a_present_control_db_directory_is_reachable(self, tmp_path: Path) -> None:
        assert control_db_unreachable_reason(tmp_path / "db.sqlite3", env=self._env(tmp_path)) is None

    def test_a_database_outside_the_control_db_directory_is_never_the_subject(self, tmp_path: Path) -> None:
        """A test DB or a per-worktree copy has no container-only mount in front of it.

        The load-bearing half of the predicate: without it every process configured
        against a private database would report the canonical mount's absence as its
        own unreachability and skip work it could perfectly well do.
        """
        assert control_db_unreachable_reason(tmp_path / "db.sqlite3", env=self._env(self._CONTAINER_ONLY)) is None

    def test_an_in_memory_database_is_reachable(self) -> None:
        assert control_db_unreachable_reason(Path(":memory:"), env=self._env(self._CONTAINER_ONLY)) is None
