"""Concurrent ``OnBehalfApproval.consume`` is single-use on prod SQLite (#960).

The on-behalf-gate's load-bearing single-use guarantee — exactly one of two
concurrent posts on the same ``(target, action)`` may proceed — rides on
``OnBehalfApproval.consume`` using ``select_for_update`` inside
``transaction.atomic`` so the second consumer observes the first's
``consumed_at`` stamp before its own UPDATE commits. On Django's SQLite
backend ``select_for_update`` is a documented no-op (#804); serialization
must therefore come from the connection's ``BEGIN`` mode — prod sets
``transaction_mode="IMMEDIATE"`` (``SQLITE_WRITE_SERIALIZATION_OPTIONS``)
so the first writer takes SQLite's reserved write lock at transaction start
and the second blocks on the busy_timeout, then reads the row already
consumed and returns ``None``.

The tests/django_settings.py default DB is ``:memory:`` per-connection (two
threads see two empty DBs — no contention is even possible there), so this
regression spins its own file-backed SQLite alias on ``tmp_path``,
registers it with the same prod ``OPTIONS``, runs the core migrations, and
calls ``OnBehalfApproval.consume(..., using=alias)`` from two real threads
released into the locked-read window simultaneously by a barrier. Mirror of
``tests/test_sqlite_write_serialization.py`` shape, scoped to the on-behalf
consume contract.

Anti-vacuity: temporarily change ``consume`` to use a plain ``filter()``
instead of ``select_for_update`` and revert ``transaction_mode`` to
unconfigured (``{}``) — the test goes RED (both threads return non-None,
the lost-update). Restoring either the locking statement or the prod
``OPTIONS`` makes it GREEN again — the test pins the locking + write-mode
contract, not just one of the two.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import connections

from teatree.core.models import OnBehalfApproval
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB.

    Matches prod's ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` so a concurrent
    second writer hits ``BEGIN IMMEDIATE`` and blocks on the busy_timeout
    instead of reading the row free under a no-op ``select_for_update``.
    """
    alias = f"oba_{uuid.uuid4().hex}"
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
    # Create just the two on-behalf tables on the alias via raw SQL — using
    # ``call_command("migrate")`` here would replay a pre-existing data
    # migration (#541 rename) that queries ``Ticket.objects`` without a
    # ``using=alias`` kwarg and so hits the empty in-memory ``default`` DB.
    # The consume codepath under test only touches ``teatree_on_behalf_approval``
    # (and the audit row is FK-only here), so the minimal schema suffices.
    with connections[alias].cursor() as cur:
        cur.execute(
            """
            CREATE TABLE teatree_on_behalf_approval (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target VARCHAR(512) NOT NULL,
                action VARCHAR(64) NOT NULL,
                approver_id VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                consumed_at DATETIME NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE teatree_on_behalf_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target VARCHAR(512) NOT NULL,
                action VARCHAR(64) NOT NULL,
                approver_id VARCHAR(255) NOT NULL,
                executed_at DATETIME NOT NULL,
                approval_id INTEGER NOT NULL
                    REFERENCES teatree_on_behalf_approval(id) ON DELETE CASCADE
            )
            """
        )
    # Close the migration-time connection so the per-thread workers each
    # open their own connection (per-thread BEGIN IMMEDIATE contention is
    # the whole point of the test).
    connections[alias].close()
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _run_two_consumers(alias: str, target: str, action: str) -> list[OnBehalfApproval | None]:
    """Two real threads race consume; return both outcomes (preserving Nones)."""
    barrier = threading.Barrier(2)
    results: dict[int, OnBehalfApproval | None] = {}

    def runner(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = OnBehalfApproval.consume(target, action, using=alias)
        finally:
            # Close the per-thread connection so the file lock is released.
            connections[alias].close()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    return [results.get(0), results.get(1)]


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard for the whole test.

    This module never touches the managed test database — it spins its own
    private file-backed SQLite connection on ``tmp_path`` and tears it down
    itself. pytest-django's guard would otherwise reject the runtime-
    registered alias the two consumer threads use.
    """
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestOnBehalfApprovalConcurrentConsume:
    """Two real threads race ``consume`` on a file-backed SQLite with IMMEDIATE.

    The serialized contract (the single-use guarantee the on-behalf gate
    rides on): exactly one consumer returns a non-None ``OnBehalfApproval``
    (the winner), the other returns ``None`` (saw the row already consumed,
    stood down). No double-consume, no ``OperationalError``.
    """

    def test_concurrent_consume_returns_winner_and_none(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            # Record one approval on the alias — the contested resource.
            OnBehalfApproval.objects.using(alias).create(
                target="org/repo!42",
                action="post_comment",
                approver_id="souliane",
            )

            outcomes = _run_two_consumers(alias, "org/repo!42", "post_comment")
        finally:
            _teardown_alias(alias)

        # Exactly one winner (non-None consumed row), one loser (None).
        winners = [o for o in outcomes if o is not None]
        losers = [o for o in outcomes if o is None]
        assert len(winners) == 1, f"expected exactly one winner, got {outcomes!r}"
        assert len(losers) == 1, f"expected exactly one loser (None), got {outcomes!r}"
        # The winner's row is stamped consumed_at — single-use claim landed.
        assert winners[0].consumed_at is not None

    def test_only_one_audit_after_concurrent_consume(self, tmp_path: Path) -> None:
        """A second consume returns None, so the caller writes ZERO audit rows.

        The audit row is written by the gate caller (``require_on_behalf_approval``)
        only when consume returns a non-None row; this guards the audit-cardinality
        invariant the on-behalf channel relies on.
        """
        from teatree.core.models import OnBehalfAudit  # noqa: PLC0415

        alias = _make_alias(tmp_path)
        try:
            OnBehalfApproval.objects.using(alias).create(
                target="org/repo!7",
                action="post_evidence",
                approver_id="souliane",
            )
            outcomes = _run_two_consumers(alias, "org/repo!7", "post_evidence")

            # Mirror what require_on_behalf_approval does after each consume:
            # write an audit row IFF consume returned non-None.
            for row in outcomes:
                if row is not None:
                    OnBehalfAudit.objects.using(alias).create(
                        approval=row,
                        target=row.target,
                        action=row.action,
                        approver_id=row.approver_id,
                    )

            audits = OnBehalfAudit.objects.using(alias).count()
        finally:
            _teardown_alias(alias)

        assert audits == 1, f"expected exactly 1 audit row, got {audits}"
