"""Concurrency regression for souliane/teatree#804.

The production database is **file-backed SQLite**
(``src/teatree/settings.py`` → ``django.db.backends.sqlite3``,
``NAME = CANONICAL_DB``).  Django's SQLite backend silently ignores
``select_for_update()`` — it is a documented no-op (SQLite has no
row-level locks).  The shared-state read-modify-write sites
(``Session.visit_phase``, ``Task.claim`` and ~12 siblings) wrap their
RMW in ``transaction.atomic()`` + ``select_for_update()`` *expecting*
mutual exclusion.  Without connection-level write serialization two
concurrent workers both ``BEGIN DEFERRED``, both read the same row,
both mutate and both commit — the lost-update those locks exist to
prevent.

This test cannot be exercised by the ordinary teatree test database:
``tests/django_settings.py`` uses ``:memory:``, which is per-connection
and single-threaded — two threads there get *two separate empty
databases*, so no contention is even possible.  This module therefore
spins its **own file-backed SQLite** on ``tmp_path``, registers two
real Django connections against it, and runs the canonical
``Task.claim``-shaped RMW (``transaction.atomic()`` +
``select_for_update().get()`` + mutate + ``save()``) from **two real
threads** that are released into the read phase simultaneously by a
barrier.

Two tests pin the two states, both run against the file-backed DB.
``test_..._red`` runs with the *pre-#804* prod config (no ``OPTIONS``):
the two writers do **not** produce the clean ``[claimed, saw-taken]``
outcome — they either double-claim (silent lost-update) or collide on
the write (``database is locked``).  This is the state the codebase was
in before #804 and the state it returns to if the settings hunk is
reverted.  ``test_..._green`` runs with the *actual production* OPTIONS
dict imported from ``teatree.settings``
(``SQLITE_WRITE_SERIALIZATION_OPTIONS``): ``BEGIN IMMEDIATE`` serializes
the writers — exactly one claims, the other observes the row already
claimed, zero ``db-locked``.

Reverting ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` to ``{}`` in
``settings.py`` makes ``..._green`` (and the settings-guard test) fail
— proving the tests guard the fix and are not vacuous.

Backend under test: **file-backed** ``django.db.backends.sqlite3``
(real ``.sqlite3`` file on disk, two OS threads, two connections).
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import OperationalError, connections, transaction

from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


def _make_alias(tmp_path: Path, options: dict[str, object]) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB."""
    alias = f"t804_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    connections.databases[alias] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_file),
        "OPTIONS": dict(options),
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "TIME_ZONE": None,
        "TEST": {},
    }
    with connections[alias].cursor() as cur:
        cur.execute("CREATE TABLE claimable (id INTEGER PRIMARY KEY, claimed_by TEXT)")
        cur.execute("INSERT INTO claimable (id, claimed_by) VALUES (1, NULL)")
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


# Per-worker outcomes of the Task.claim-shaped RMW.
_CLAIMED = "claimed"  # locked read saw row free, UPDATE+commit succeeded
_SAW_TAKEN = "saw-taken"  # locked read saw row already claimed
_DB_LOCKED = "db-locked"  # SQLite refused the contended write


def _claim_like_task_claim(alias: str, worker: str, barrier: threading.Barrier) -> str:
    """Mirror the ``Task.claim`` RMW: locked re-read, decide, write, commit.

    Returns the worker's outcome. ``_CLAIMED`` — the locked re-read saw
    ``claimed_by IS NULL`` and the worker's ``UPDATE`` committed.
    ``_SAW_TAKEN`` — the locked re-read saw the row already claimed, so
    the worker stood down (the correct, serialized outcome for the
    loser). ``_DB_LOCKED`` — SQLite raised ``database is locked`` on the
    contended write: the inert-locking failure mode the unserialized
    ``BEGIN DEFERRED`` path produces when two writers collide.

    The barrier releases both workers into the read-modify-write window
    simultaneously so the contention is real, not serialized by
    thread-start latency.  The 50ms hold inside the ``atomic()`` block
    widens the window so an unserialized second writer reliably reads the
    still-NULL row before the first commits.

    The serialized contract (``Task.claim`` / ``Session.visit_phase``
    expect from ``select_for_update()``): exactly one worker returns
    ``_CLAIMED``, the other ``_SAW_TAKEN``, and **no** worker returns
    ``_DB_LOCKED``.  Any other distribution is the lost-update /
    double-claim / inert-lock the issue describes.
    """
    conn = connections[alias]
    try:
        barrier.wait(timeout=10)
        with transaction.atomic(using=alias), conn.cursor() as cur:
            # The select_for_update()-shaped locked re-read.  On the
            # production engine the clause is a no-op; serialization
            # must come from the connection BEGIN mode instead.
            cur.execute("SELECT claimed_by FROM claimable WHERE id = 1")
            row = cur.fetchone()
            assert row is not None  # the id=1 row is INSERTed in _make_alias
            if row[0] is not None:
                return _SAW_TAKEN
            threading.Event().wait(0.05)
            cur.execute("UPDATE claimable SET claimed_by = %s WHERE id = 1", [worker])
            return _CLAIMED
    except OperationalError:
        return _DB_LOCKED
    finally:
        conn.close()


def _run_two_writers(alias: str) -> list[str]:
    """Two real threads race the claim; return both workers' outcomes."""
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def runner(name: str) -> None:
        results[name] = _claim_like_task_claim(alias, name, barrier)

    threads = [threading.Thread(target=runner, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    return sorted(results.values())


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard for the whole test.

    This module never touches the managed test database — it spins its
    own private file-backed SQLite connections on ``tmp_path`` and tears
    them down itself.  pytest-django's guard (and the ``TestCase``
    threaded-connection allowlist) would otherwise reject the
    runtime-registered aliases the two writer threads use.  No
    ``django_db`` marker is used so no rollback wrapper interferes with
    the real cross-connection commits this test depends on.
    """
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestSqliteWriteSerialization:
    """Two real threads contend on a row in a file-backed SQLite DB."""

    _SERIALIZED = sorted([_CLAIMED, _SAW_TAKEN])

    def test_pre_804_prod_config_does_not_serialize_writers_red(
        self,
        tmp_path: Path,
    ) -> None:
        """The pre-#804 prod config (no OPTIONS) fails to serialize.

        ``src/teatree/settings.py`` before #804 declared the SQLite
        ``default`` database with **no OPTIONS** — no ``timeout``, no
        ``init_command``, no ``transaction_mode``.  Under that config two
        concurrent ``Task.claim``-shaped writers do **not** produce the
        clean ``[claimed, saw-taken]`` outcome ``select_for_update()`` is
        supposed to guarantee: they either both read the row free and
        double-claim, or collide on the write and one gets ``database is
        locked``.  Either way the serialized contract is violated — which
        is exactly why the locking strategy is inert on prod SQLite.
        """
        alias = _make_alias(tmp_path, {})  # pre-#804 prod: no OPTIONS
        try:
            outcomes = _run_two_writers(alias)
        finally:
            _teardown_alias(alias)

        assert outcomes != self._SERIALIZED, f"pre-#804 config unexpectedly serialized writers: {outcomes}"
        # Concretely it is one of the broken distributions: a silent
        # lost-update / double-claim, or an inert-lock write collision.
        broken = {
            (_CLAIMED, _CLAIMED),
            tuple(sorted([_CLAIMED, _DB_LOCKED])),
            (_DB_LOCKED, _DB_LOCKED),
        }
        assert tuple(outcomes) in broken, outcomes

    def test_production_options_serialize_concurrent_writers_green(
        self,
        tmp_path: Path,
    ) -> None:
        """The #804 production OPTIONS serialize the two writers.

        With ``transaction_mode="IMMEDIATE"`` the first writer's ``BEGIN
        IMMEDIATE`` takes SQLite's reserved write lock at transaction
        start; the second writer blocks on the busy_timeout until the
        first commits, then its locked re-read observes the row already
        claimed and it stands down.  Exactly one ``claimed``, one
        ``saw-taken``, zero ``db-locked`` — the contract
        ``select_for_update()`` is written to provide.

        Reverting ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` to ``{}`` in
        ``settings.py`` makes this parametrization run the pre-#804
        config and the assertion fails — proving the test guards the fix
        and is not vacuous.
        """
        alias = _make_alias(tmp_path, SQLITE_WRITE_SERIALIZATION_OPTIONS)
        try:
            outcomes = _run_two_writers(alias)
        finally:
            _teardown_alias(alias)

        assert outcomes == self._SERIALIZED, f"production OPTIONS failed to serialize writers: {outcomes}"

    def test_production_settings_declare_write_serialization(self) -> None:
        """The prod DATABASES['default'] carries the serialization OPTIONS.

        Guards against a silent revert of the settings hunk: the engine
        is SQLite and the OPTIONS must contain IMMEDIATE transaction
        mode, a busy_timeout, and WAL journal mode.
        """
        from teatree import settings as prod_settings  # noqa: PLC0415

        default = prod_settings.DATABASES["default"]
        assert default["ENGINE"] == "django.db.backends.sqlite3"
        opts = default["OPTIONS"]
        assert opts is SQLITE_WRITE_SERIALIZATION_OPTIONS
        assert opts["transaction_mode"] == "IMMEDIATE"
        assert opts["timeout"] == 30
        assert "journal_mode=WAL" in opts["init_command"]
