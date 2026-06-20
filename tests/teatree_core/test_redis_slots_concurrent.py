"""Concurrent ``allocate_redis_slot`` never double-claims a slot on prod SQLite.

The slot allocator's contract: two concurrent worktree provisions must never
land on the same Redis DB index, and the loser must reselect the next free
slot rather than crash. ``redis_db_index`` carries a ``unique=True``
constraint, so the pre-fix read-taken-set → pick-lowest-free → ``save()``
shape lets two callers both read the same taken-set, both pick the same free
index, and the loser's ``save()`` raises an uncaught ``IntegrityError`` — a
TOCTOU double-claim.

On Django's SQLite backend ``select_for_update`` is a documented no-op
(#804); serialization comes from the connection's ``BEGIN`` mode. Prod sets
``transaction_mode="IMMEDIATE"`` (``SQLITE_WRITE_SERIALIZATION_OPTIONS``) so
the first writer takes SQLite's reserved write lock at transaction start and
the second blocks on the busy_timeout, then re-reads the taken-set with the
first's slot already present and reselects. Mirror of
``tests/teatree_core/test_on_behalf_approval_concurrent.py`` shape, scoped to
the redis-slot claim.

Anti-vacuity: revert ``allocate_redis_slot`` to the unguarded
read-pick-save (no ``transaction.atomic`` + caught ``IntegrityError`` retry)
and the test goes RED — the loser raises ``IntegrityError`` (or both land on
slot 0). Restoring the atomic claim makes it GREEN: distinct slots, no
``IntegrityError`` escapes.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import connections

from teatree.core.modelkit.errors import RedisSlotsExhaustedError
from teatree.core.models import Ticket
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB.

    Matches prod's ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` so a concurrent
    second writer hits ``BEGIN IMMEDIATE`` and blocks on the busy_timeout
    instead of reading the taken-set free under a no-op ``select_for_update``.
    """
    alias = f"redis_slot_{uuid.uuid4().hex}"
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
        # Hand-maintained mirrors of the schemas queried by allocate_redis_slot
        # and release_orphaned_redis_slots — add any new column here too.
        cur.execute(
            """
            CREATE TABLE teatree_ticket (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                overlay VARCHAR(255) NOT NULL DEFAULT '',
                issue_url VARCHAR(500) NOT NULL DEFAULT '',
                variant VARCHAR(100) NOT NULL DEFAULT '',
                repos TEXT NOT NULL DEFAULT '[]',
                state VARCHAR(32) NOT NULL DEFAULT 'not_started',
                role VARCHAR(16) NOT NULL DEFAULT 'author',
                kind VARCHAR(16) NOT NULL DEFAULT 'feature',
                extra TEXT NOT NULL DEFAULT '{}',
                context TEXT NOT NULL DEFAULT '',
                short_description VARCHAR(80) NOT NULL DEFAULT '',
                redis_db_index INTEGER NULL UNIQUE,
                remote_missing BOOLEAN NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE teatree_worktree (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                overlay VARCHAR(255) NOT NULL DEFAULT '',
                ticket_id INTEGER NOT NULL REFERENCES teatree_ticket(id),
                repo_path VARCHAR(500) NOT NULL DEFAULT '',
                branch VARCHAR(255) NOT NULL DEFAULT '',
                state VARCHAR(32) NOT NULL DEFAULT 'created',
                db_name VARCHAR(255) NOT NULL DEFAULT '',
                extra TEXT NOT NULL DEFAULT '{}',
                last_used_at DATETIME NULL,
                last_e2e_run DATETIME NULL
            )
            """
        )
    connections[alias].close()
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _run_two_allocators(alias: str, pks: tuple[int, int]) -> dict[int, int | Exception]:
    """Two real threads race ``allocate_redis_slot`` on distinct tickets."""
    barrier = threading.Barrier(2)
    results: dict[int, int | Exception] = {}

    def runner(idx: int, pk: int) -> None:
        try:
            ticket = Ticket.objects.using(alias).get(pk=pk)
            barrier.wait(timeout=10)
            try:
                results[idx] = Ticket.objects.using(alias).allocate_redis_slot(ticket)
            except Exception as exc:  # noqa: BLE001 — record so the assertion can see an IntegrityError escape
                results[idx] = exc
        finally:
            connections[alias].close()

    threads = [threading.Thread(target=runner, args=(i, pks[i])) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    return results


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard for the whole test.

    This module spins its own private file-backed SQLite connection on
    ``tmp_path`` and tears it down itself, so the runtime-registered alias
    the two allocator threads use must bypass pytest-django's guard.
    """
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestAllocateRedisSlotConcurrent:
    """Two real threads race ``allocate_redis_slot`` on a file-backed SQLite with IMMEDIATE."""

    def test_concurrent_claims_get_distinct_slots_without_integrity_error(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            a = Ticket.objects.using(alias).create()
            b = Ticket.objects.using(alias).create()
            results = _run_two_allocators(alias, (a.pk, b.pk))
        finally:
            _teardown_alias(alias)

        errors = [r for r in results.values() if isinstance(r, Exception)]
        assert not errors, f"a concurrent claim raised instead of reselecting: {errors!r}"
        slots = sorted(r for r in results.values() if isinstance(r, int))
        assert slots == [0, 1], f"expected the two claims to take distinct slots, got {slots!r}"

    def test_loser_reselects_next_free_slot_when_lowest_is_taken(self, tmp_path: Path) -> None:
        """With slot 0 pre-taken, two racers must split 1 and 2 — no collision, no raise."""
        alias = _make_alias(tmp_path)
        try:
            pre = Ticket.objects.using(alias).create()
            Ticket.objects.using(alias).allocate_redis_slot(pre)
            a = Ticket.objects.using(alias).create()
            b = Ticket.objects.using(alias).create()
            results = _run_two_allocators(alias, (a.pk, b.pk))
        finally:
            _teardown_alias(alias)

        errors = [r for r in results.values() if isinstance(r, Exception)]
        assert not errors, f"a concurrent claim raised instead of reselecting: {errors!r}"
        slots = sorted(r for r in results.values() if isinstance(r, int))
        assert slots == [1, 2], f"expected the racers to take the next two free slots, got {slots!r}"


@pytest.mark.usefixtures("_unblocked_db")
class TestAllocateRedisSlotConcurrentExhaustion:
    """The exhaustion contract survives concurrency: the loser of the last slot raises cleanly."""

    def test_last_free_slot_contended_yields_one_winner_and_one_exhausted(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            from teatree.config import load_config  # noqa: PLC0415

            count = load_config().user.redis_db_count
            taken = [Ticket.objects.using(alias).create() for _ in range(count - 1)]
            for ticket in taken:
                Ticket.objects.using(alias).allocate_redis_slot(ticket)
                # Give each pre-allocated ticket a live-path Worktree row so
                # release_orphaned_redis_slots does not treat them as ghosts and
                # reclaim them before the two racing allocators contend for the
                # last slot.
                from django.db import connections as _conns  # noqa: PLC0415

                with _conns[alias].cursor() as cur:
                    cur.execute(
                        "INSERT INTO teatree_worktree (ticket_id, overlay, repo_path, branch, extra)"
                        " VALUES (?, '', 'org/repo', 'main', ?)",
                        [ticket.pk, f'{{"worktree_path": "{tmp_path}"}}'],
                    )
            a = Ticket.objects.using(alias).create()
            b = Ticket.objects.using(alias).create()
            results = _run_two_allocators(alias, (a.pk, b.pk))
        finally:
            _teardown_alias(alias)

        winners = [r for r in results.values() if isinstance(r, int)]
        exhausted = [r for r in results.values() if isinstance(r, RedisSlotsExhaustedError)]
        other = [
            r for r in results.values() if isinstance(r, Exception) and not isinstance(r, RedisSlotsExhaustedError)
        ]
        assert not other, f"unexpected exception type from a contended last-slot claim: {other!r}"
        assert len(winners) == 1, f"expected exactly one winner of the last free slot, got {results!r}"
        assert len(exhausted) == 1, f"expected the loser to raise RedisSlotsExhaustedError, got {results!r}"
