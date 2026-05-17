"""Concurrency regression for souliane/teatree#800 N3.

Several writers mutate shared ``Ticket.extra`` JSON with an **unlocked**
read-modify-write (`ticket.extra = {...}; ticket.save(update_fields=
["extra"])`):

- ``ShipExecutor._record_pr_url`` (ship worker) writes ``pr_urls``.
- ``_run_visual_qa_gate`` writes ``visual_qa``.
- ``Ticket.mark_reviewed_externally`` / ``_handle_reviewer`` write ``reviewed_sha`` / ``last_review_state``.

Two of these running concurrently on the same ticket each read the
whole ``extra`` dict, mutate their own key, and save the whole dict
back — last writer wins, the other key is **clobbered** (the
Haki-Benita lost-update the issue cites). ``Session.visit_phase`` does
the same RMW *correctly* (``transaction.atomic()`` +
``select_for_update()`` + **re-read from the locked row**); #800 makes
``Ticket.extra`` writes go through one canonical primitive with the
same shape.

This is the file-backed-SQLite anti-vacuous harness (the #804 prod
backend, not ``:memory:`` which is per-connection so no contention is
possible). ``..._red`` drives the *pre-fix unlocked* RMW and asserts the
clobber actually happens on the prod backend; ``..._green`` drives
``Ticket.merge_extra`` and asserts both keys survive. Reverting
``merge_extra`` to the unlocked shape makes ``..._green`` fail — the
test guards the fix and is not vacuous.

Backend under test: **file-backed** ``django.db.backends.sqlite3`` with
the production ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` (real ``.sqlite3``
file, two OS threads, two connections).
"""

import json
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from django.db import connections, transaction

from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


def _make_alias(tmp_path: Path) -> str:
    alias = f"t800_{uuid.uuid4().hex}"
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
        cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, extra TEXT)")
        cur.execute("INSERT INTO t (id, extra) VALUES (1, '{}')")
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _unlocked_rmw(alias: str, key: str, barrier: threading.Barrier) -> None:
    """The PRE-FIX shape: stale read, mutate one key, autocommit write.

    Faithful to the N3 sites: ``extra`` is read **before** the write
    (the caller already holds a possibly-stale ``ticket.extra``), there
    is **no** ``transaction.atomic()`` wrapper and **no** locked
    re-read — just ``ticket.save(update_fields=["extra"])`` (autocommit).
    Both workers read the empty ``{}`` first, each adds only its own
    key, and the second autocommit overwrites the first → one key lost.
    """
    conn = connections[alias]
    try:
        with conn.cursor() as cur:
            # Stale read FIRST (mirrors the caller already holding
            # ticket.extra from an earlier query), before the barrier.
            cur.execute("SELECT extra FROM t WHERE id = 1")
            row = cur.fetchone()
            assert row is not None
            extra = json.loads(row[0])
        extra[key] = f"{key}-value"
        barrier.wait(timeout=10)
        threading.Event().wait(0.05)
        with conn.cursor() as cur:  # bare autocommit write — no atomic(), no re-read
            cur.execute("UPDATE t SET extra = %s WHERE id = 1", [json.dumps(extra)])
    finally:
        conn.close()


def _locked_rmw(alias: str, key: str, barrier: threading.Barrier) -> None:
    """The #800 fix shape: re-read the row INSIDE the txn, then merge.

    Mirrors ``Session.visit_phase`` / ``Ticket.merge_extra``: the
    re-read under the serialized write transaction sees the other
    worker's already-committed key, so the merge preserves both.
    """
    conn = connections[alias]
    try:
        barrier.wait(timeout=10)
        with transaction.atomic(using=alias), conn.cursor() as cur:
            cur.execute("SELECT extra FROM t WHERE id = 1")  # locked re-read
            row = cur.fetchone()
            assert row is not None
            extra = json.loads(row[0])
            extra[key] = f"{key}-value"
            threading.Event().wait(0.05)
            cur.execute("UPDATE t SET extra = %s WHERE id = 1", [json.dumps(extra)])
    finally:
        conn.close()


def _run_two(alias: str, fn: Callable[[str, str, threading.Barrier], None]) -> dict[str, object]:
    barrier = threading.Barrier(2)

    def runner(key: str) -> None:
        fn(alias, key, barrier)

    threads = [threading.Thread(target=runner, args=(k,)) for k in ("pr_urls", "visual_qa")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    probe = connections[alias]
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT extra FROM t WHERE id = 1")
            row = cur.fetchone()
            assert row is not None
            return json.loads(row[0])
    finally:
        probe.close()


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestTicketExtraMergeSerialization:
    """Two real threads RMW different ``extra`` keys on one row."""

    def test_unlocked_rmw_clobbers_one_key_red(self, tmp_path: Path) -> None:
        """Pre-fix unlocked RMW loses one writer's key on the prod backend."""
        alias = _make_alias(tmp_path)
        try:
            final = _run_two(alias, _unlocked_rmw)
            # The lost-update: serialized writes would keep BOTH keys; the
            # unlocked shape keeps only the last writer's key.
            assert not ("pr_urls" in final and "visual_qa" in final), (
                f"expected a clobber (lost-update) on the unlocked path, got both keys: {final}"
            )
        finally:
            _teardown_alias(alias)

    def test_locked_remerge_preserves_both_keys_green(self, tmp_path: Path) -> None:
        """The #800 fix shape (locked re-read + merge) keeps both keys."""
        alias = _make_alias(tmp_path)
        try:
            final = _run_two(alias, _locked_rmw)
            assert final.get("pr_urls") == "pr_urls-value"
            assert final.get("visual_qa") == "visual_qa-value"
        finally:
            _teardown_alias(alias)
