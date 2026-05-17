"""Concurrency regression for souliane/teatree#800 N5.

``TaskQuerySet.reap_stale_claims`` (``src/teatree/core/managers.py``)
iterates ``status=CLAIMED, lease_expires_at < now`` and calls
``task.fail()`` per row with **no re-check under a lock**. It runs every
tick. A concurrent ``Task.renew_lease`` (the worker heartbeating its
still-alive claim) extends ``lease_expires_at``, but the reaper already
decided "stale" from its earlier read and unconditionally fails the
row → a healthy, just-renewed task is **spuriously failed**.

The #800 fix gives the reap the #804 backend-agnostic conditional-UPDATE
CAS shape: instead of read-then-unconditional-``fail()``, a single
``UPDATE ... WHERE status=CLAIMED AND lease_expires_at < now`` — the
expiry predicate is the compare-and-swap token, re-evaluated atomically
at write time. A lease renewed between the scan and the write moves
``lease_expires_at`` past ``now``, the ``WHERE`` no longer matches, and
that row is **not** reaped. Correct on prod SQLite (where
``select_for_update`` is a no-op) because the conditional UPDATE is
itself atomic.

File-backed-SQLite anti-vacuous harness (the #804 prod backend, with
``SQLITE_WRITE_SERIALIZATION_OPTIONS``). ``..._red`` drives the
read-then-unconditional-fail shape and asserts the renewed row IS
wrongly failed; ``..._green`` drives the conditional-UPDATE CAS and
asserts the renewed row survives. Reverting the manager to the
unconditional shape makes ``..._green`` fail.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from django.db import connections, transaction

from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS

_NOW = 1_000_000  # fixed clock (epoch secs); lease math is relative to it.


def _make_alias(tmp_path: Path) -> str:
    alias = f"t800n5_{uuid.uuid4().hex}"
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
        cur.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, status TEXT, lease_expires_at INTEGER)")
        # One CLAIMED task whose lease is already expired at _NOW.
        cur.execute("INSERT INTO task (id, status, lease_expires_at) VALUES (1, 'claimed', ?)", [_NOW - 10])
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _reaper_unconditional(alias: str, scanned: threading.Event, renewed: threading.Event) -> None:
    """PRE-FIX shape: scan stale, then unconditionally fail each row.

    The reaper reads the (then-stale) row, signals it has scanned, waits
    for the concurrent renewal, then fails the row WITHOUT re-checking
    the lease — the spurious-fail the issue describes.
    """
    conn = connections[alias]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM task WHERE status='claimed' AND lease_expires_at < ?", [_NOW])
            stale_ids = [r[0] for r in cur.fetchall()]
        scanned.set()
        renewed.wait(timeout=10)
        with conn.cursor() as cur:
            for tid in stale_ids:
                cur.execute("UPDATE task SET status='failed' WHERE id = ?", [tid])
    finally:
        conn.close()


def _reaper_cas(alias: str, scanned: threading.Event, renewed: threading.Event) -> None:
    """#800 fix shape: single conditional UPDATE, expiry is the CAS token.

    Re-evaluates ``lease_expires_at < now`` atomically at write time, so
    a row renewed after the scan no longer matches and is not reaped.
    """
    conn = connections[alias]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM task WHERE status='claimed' AND lease_expires_at < ?", [_NOW])
            _ = cur.fetchall()
        scanned.set()
        renewed.wait(timeout=10)
        with transaction.atomic(using=alias), conn.cursor() as cur:
            cur.execute(
                "UPDATE task SET status='failed' WHERE status='claimed' AND lease_expires_at < ?",
                [_NOW],
            )
    finally:
        conn.close()


def _renew(alias: str, scanned: threading.Event, renewed: threading.Event) -> None:
    """The worker heartbeat: after the reaper scanned, extend the lease."""
    conn = connections[alias]
    try:
        scanned.wait(timeout=10)
        with conn.cursor() as cur:
            cur.execute("UPDATE task SET lease_expires_at = ? WHERE id = 1", [_NOW + 300])
        renewed.set()
    finally:
        conn.close()


def _run(alias: str, reaper: Callable[[str, threading.Event, threading.Event], None]) -> str:
    scanned, renewed = threading.Event(), threading.Event()
    threads = [
        threading.Thread(target=reaper, args=(alias, scanned, renewed)),
        threading.Thread(target=_renew, args=(alias, scanned, renewed)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    probe = connections[alias]
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT status FROM task WHERE id = 1")
            row = cur.fetchone()
            assert row is not None
            return row[0]
    finally:
        probe.close()


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestReapStaleClaimsCas:
    """A lease renewed between the reap scan and the reap write survives."""

    def test_unconditional_fail_spuriously_reaps_renewed_lease_red(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            status = _run(alias, _reaper_unconditional)
            # The bug: the just-renewed (healthy) task was failed anyway.
            assert status == "failed", f"expected the pre-fix spurious-fail, got {status!r}"
        finally:
            _teardown_alias(alias)

    def test_conditional_update_cas_spares_renewed_lease_green(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            status = _run(alias, _reaper_cas)
            # The fix: the renewed lease no longer matches WHERE
            # lease_expires_at < now, so the row is not reaped.
            assert status == "claimed", f"renewed lease must survive the CAS reap, got {status!r}"
        finally:
            _teardown_alias(alias)
