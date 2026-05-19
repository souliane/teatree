"""``answered_at`` stamping is race-safe on the prod SQLite backend (#1063).

Per ``feedback_db_concurrency_test_on_prod_backend.md`` and the
``t3:code`` TDD doctrine: the locking/race test for a state-stamp must
run on the **production DB backend** (file-backed SQLite) and model the
pre-fix code's *actual* transaction shape — the unlocked read-modify-
write (bare autocommit ``save()`` from a possibly-stale in-memory read),
NOT an ``atomic()``-wrapped approximation that the prod backend's
connection-level write serialization would silently mask.

``PendingChatInjection.agent_answered_question`` is implemented as a
single conditional UPDATE:

    UPDATE … SET answered_at = now WHERE id = ? AND answered_at IS NULL

This is the correct race-safe shape — the ``WHERE answered_at IS NULL``
guard is evaluated atomically inside the one UPDATE statement, so two
concurrent stampers cannot both transition the row. The contrast test
below proves the point by also exercising the **unlocked-RMW** shape a
naive implementation would have used (read ``answered_at`` into Python,
branch in Python, then a bare autocommit ``UPDATE``): on the
file-backed prod engine that shape double-stamps (both writers observe
NULL before either commits), which is exactly the lost-update class the
conditional UPDATE avoids.

This module spins its own private file-backed SQLite connections (mirror
of ``test_sqlite_write_serialization.py``); it never touches the managed
test DB and uses no ``django_db`` marker so no rollback wrapper
interferes with the real cross-connection commits.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import OperationalError, connections

# The production SQLite OPTIONS (the #804 connection-level write
# serialization). The race test must run against the SAME options the
# prod config ships, not a bare ``{}`` — otherwise it tests a backend
# the user never runs.
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection on a fresh file-backed SQLite DB with prod OPTIONS."""
    alias = f"ans_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    connections.databases[alias] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_file),
        "OPTIONS": dict(SQLITE_WRITE_SERIALIZATION_OPTIONS),
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {},
    }
    with connections[alias].cursor() as cur:
        cur.execute("CREATE TABLE answerable (id INTEGER PRIMARY KEY, answered_at TEXT)")
        cur.execute("INSERT INTO answerable (id, answered_at) VALUES (1, NULL)")
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


_STAMPED = "stamped"  # this worker's conditional UPDATE transitioned the row (1 row)
_NOOP = "noop"  # the row was already stamped; this worker changed 0 rows
_DB_LOCKED = "db-locked"  # SQLite refused the contended write


def _conditional_update_stamp(alias: str, worker: str, barrier: threading.Barrier) -> str:
    """Mirror ``agent_answered_question``: ONE conditional UPDATE, bare autocommit.

    No ``transaction.atomic()`` wrapper — exactly the production code
    path (``cls.objects.filter(answered_at__isnull=True).update(...)``
    is a single autocommit statement). The ``WHERE answered_at IS NULL``
    guard is inside the UPDATE, so the row-count return distinguishes the
    winner (1) from the loser (0) atomically.
    """
    conn = connections[alias]
    try:
        barrier.wait(timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE answerable SET answered_at = %s WHERE id = 1 AND answered_at IS NULL",
                [f"now-{worker}"],
            )
            return _STAMPED if cur.rowcount == 1 else _NOOP
    except OperationalError:
        return _DB_LOCKED
    finally:
        conn.close()


def _unlocked_rmw_stamp(alias: str, worker: str, barrier: threading.Barrier) -> str:
    """The NAIVE shape: stale Python read, branch in Python, bare autocommit write.

    This is the lost-update class the conditional UPDATE avoids. No
    ``transaction.atomic()`` wrapper (matches the defect's real
    concurrency primitive — autocommit, stale read first). On the
    file-backed prod engine both writers read ``answered_at IS NULL``
    before either commits, so both proceed to stamp: a double-stamp.
    """
    conn = connections[alias]
    try:
        barrier.wait(timeout=10)
        with conn.cursor() as cur:
            cur.execute("SELECT answered_at FROM answerable WHERE id = 1")
            row = cur.fetchone()
        assert row is not None  # the id=1 row is INSERTed in _make_alias
        if row[0] is not None:
            return _NOOP
        # Widen the window so the second (still-stale) reader is inside
        # it before the first commits — the lost-update reproduction.
        threading.Event().wait(0.05)
        with conn.cursor() as cur:
            cur.execute("UPDATE answerable SET answered_at = %s WHERE id = 1", [f"now-{worker}"])
    except OperationalError:
        return _DB_LOCKED
    else:
        return _STAMPED
    finally:
        conn.close()


def _race(alias: str, fn) -> list[str]:
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def runner(name: str) -> None:
        results[name] = fn(alias, name, barrier)

    threads = [threading.Thread(target=runner, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return sorted(results.values())


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestAnsweredAtStampConcurrency:
    """Two real threads contend on the same row, on the prod SQLite engine."""

    def test_conditional_update_serializes_exactly_one_winner_green(self, tmp_path: Path) -> None:
        """The production ``agent_answered_question`` shape: exactly one stamps.

        With the conditional UPDATE, the contended write is one atomic
        statement guarded by ``WHERE answered_at IS NULL``. On the prod
        OPTIONS the second writer blocks on busy_timeout, then its own
        UPDATE matches zero rows (the guard now fails) and returns
        ``noop``. Outcome: exactly ``[noop, stamped]`` — never two
        stamps, never a ``db-locked``.
        """
        alias = _make_alias(tmp_path)
        try:
            outcomes = _race(alias, _conditional_update_stamp)
            with connections[alias].cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM answerable WHERE answered_at IS NOT NULL")
                count_row = cur.fetchone()
            assert count_row is not None
            stamped_rows = count_row[0]
        finally:
            _teardown_alias(alias)

        assert outcomes == sorted([_NOOP, _STAMPED]), outcomes
        assert stamped_rows == 1

    def test_unlocked_rmw_double_stamps_red(self, tmp_path: Path) -> None:
        """Contrast: the naive read-then-write shape double-stamps.

        This proves the conditional UPDATE in the production code is
        load-bearing, not incidental: the unlocked-RMW alternative — a
        stale Python read, a Python branch, then a bare autocommit
        UPDATE with no guard — lets BOTH writers observe ``NULL`` and
        both stamp. ``[stamped, stamped]`` is the lost-update the real
        implementation avoids.
        """
        alias = _make_alias(tmp_path)
        try:
            outcomes = _race(alias, _unlocked_rmw_stamp)
        finally:
            _teardown_alias(alias)

        # The naive shape does NOT achieve the serialized contract.
        assert outcomes != sorted([_NOOP, _STAMPED]), outcomes
        assert outcomes == sorted([_STAMPED, _STAMPED]), outcomes
