"""K-claimer x M-row concurrency for the ``claim_next_pending`` CAS (#786 / #1796).

Agent-teams Track-A PR#1 arms ``orchestrate_phase`` to claim N dispatchable
rows per tick through ``Task.objects.claim_next_pending``. With several leads
(or several ticks) running concurrently, the contract that must hold is: K
concurrent claimers over M pending rows claim **exactly M** rows, every one
**distinct** — zero double-claims, zero lost rows.

The single-row interleave (``test_managers.TestClaimNextPendingConcurrencyOnSqlite``)
already pins the 2-claimer / 1-row race. This module generalises it to K real
threads over M rows on a **file-backed** SQLite DB under the actual production
OPTIONS (``BEGIN IMMEDIATE``) — the same harness shape as
``test_sqlite_write_serialization.py``, because the managed ``:memory:`` test DB
is per-connection and cannot host cross-thread contention.

It mirrors ``claim_next_pending``'s exact CAS in raw SQL (``SELECT oldest
PENDING -> conditional UPDATE ... WHERE status='pending'``) so the test exercises
the production claim shape, not an ``atomic()``-wrapped approximation. Running it
with the pre-#804 config (no OPTIONS) reproduces double-claims / db-locked — the
RED that proves the test is not vacuous (covered by
``test_pre_804_config_double_claims_over_m_rows``).
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import OperationalError, connections, transaction

from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS

_M_ROWS = 5
_K_CLAIMERS = 8  # more claimers than rows: the surplus must claim nothing


def _make_alias(tmp_path: Path, options: dict[str, object]) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB with M pending rows."""
    alias = f"claimkm_{uuid.uuid4().hex}"
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
        cur.execute(
            "CREATE TABLE claimable (id INTEGER PRIMARY KEY, status TEXT, claimed_by TEXT, claimed_by_session TEXT)",
        )
        for i in range(1, _M_ROWS + 1):
            cur.execute(
                "INSERT INTO claimable (id, status, claimed_by, claimed_by_session) VALUES (%s, 'pending', NULL, '')",
                [i],
            )
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _claim_next_pending_like(alias: str, worker: str, *, session: str = "") -> int | None:
    """Mirror ``Task.objects.claim_next_pending``'s CAS: select oldest PENDING, conditional UPDATE.

    ``session`` rides the UPDATE's SET clause only — exactly like the production
    ``claimed_by_session`` (#1917). The CAS ``WHERE status = 'pending'``
    predicate is untouched by it, so the claim semantics are identical with or
    without a session.

    Returns the claimed row id, or ``None`` when nothing was claimable / the
    CAS lost the race (UPDATE matched 0 rows) / the contended write was refused
    (``database is locked``). Exactly the three outcomes the production manager
    folds into "claimed task or None".
    """
    conn = connections[alias]
    try:
        with transaction.atomic(using=alias), conn.cursor() as cur:
            cur.execute("SELECT id FROM claimable WHERE status = 'pending' ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row is None:
                return None
            oldest = row[0]
            # Widen the window so an unserialized rival reads the same oldest
            # row as still-pending before this writer commits.
            threading.Event().wait(0.02)
            cur.execute(
                "UPDATE claimable SET status = 'claimed', claimed_by = %s, claimed_by_session = %s "
                "WHERE id = %s AND status = 'pending'",
                [worker, session, oldest],
            )
            return oldest if cur.rowcount == 1 else None
    except OperationalError:
        return None
    finally:
        conn.close()


def _run_k_claimers(alias: str, *, sessions: dict[str, str] | None = None) -> list[int | None]:
    """K real threads each call the claim CAS once, released together by a barrier.

    When ``sessions`` is given it maps each claimer name to the distinct session
    that claimer supplies, so each claimed row carries exactly one winner's
    session (#1917). Without it the claimers supply no session (empty string).
    """
    barrier = threading.Barrier(_K_CLAIMERS)
    results: dict[str, int | None] = {}

    def runner(name: str) -> None:
        barrier.wait(timeout=10)
        session = sessions.get(name, "") if sessions else ""
        results[name] = _claim_next_pending_like(alias, name, session=session)

    threads = [threading.Thread(target=runner, args=(f"tick-{i}",)) for i in range(_K_CLAIMERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return list(results.values())


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard — this module owns its file-backed connections."""
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestClaimNextPendingKClaimersMRows:
    """K real threads race the claim CAS over M file-backed rows."""

    def test_k_claimers_over_m_rows_claim_exactly_m_with_no_double_claims(self, tmp_path: Path) -> None:
        """Production OPTIONS: exactly M distinct rows claimed, surplus claimers get None."""
        alias = _make_alias(tmp_path, SQLITE_WRITE_SERIALIZATION_OPTIONS)
        try:
            outcomes = _run_k_claimers(alias)
            claimed = [r for r in outcomes if r is not None]
            with connections[alias].cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM claimable WHERE status = 'claimed'")
                count_row = cur.fetchone()
                assert count_row is not None  # COUNT(*) always returns one row
                db_claimed = count_row[0]
        finally:
            _teardown_alias(alias)

        # Exactly M rows handed out across the K claimers, every one distinct.
        assert len(claimed) == _M_ROWS, f"expected {_M_ROWS} claims, got {len(claimed)}: {outcomes}"
        assert len(set(claimed)) == _M_ROWS, f"a row was claimed twice: {claimed}"
        # The K - M surplus claimers each got None (nothing left / lost the CAS).
        assert outcomes.count(None) == _K_CLAIMERS - _M_ROWS
        # The DB agrees: exactly M rows are CLAIMED.
        assert db_claimed == _M_ROWS

    def test_each_claimed_row_carries_exactly_one_winners_session(self, tmp_path: Path) -> None:
        """#1917: K distinct sessions race; each of the M claimed rows carries one winner's session.

        Each claimer supplies a distinct ``claimed_by_session`` on the SET clause.
        Because the session never touches the CAS predicate, the claim outcome is
        unchanged — exactly M distinct rows claimed — and every claimed row's
        session is the one supplied by the claimer that won it (no cross-claim
        bleed). The K - M surplus claimers get nothing.
        """
        sessions = {f"tick-{i}": f"sess-{i}" for i in range(_K_CLAIMERS)}
        alias = _make_alias(tmp_path, SQLITE_WRITE_SERIALIZATION_OPTIONS)
        try:
            outcomes = _run_k_claimers(alias, sessions=sessions)
            claimed = [r for r in outcomes if r is not None]
            with connections[alias].cursor() as cur:
                cur.execute(
                    "SELECT claimed_by, claimed_by_session FROM claimable WHERE status = 'claimed' ORDER BY id",
                )
                claimed_rows = cur.fetchall()
        finally:
            _teardown_alias(alias)

        assert len(claimed) == _M_ROWS, f"expected {_M_ROWS} claims, got {len(claimed)}: {outcomes}"
        assert len(set(claimed)) == _M_ROWS, f"a row was claimed twice: {claimed}"
        assert outcomes.count(None) == _K_CLAIMERS - _M_ROWS
        assert len(claimed_rows) == _M_ROWS
        # Each claimed row's session is the distinct one the winning claimer
        # supplied — the worker label and its session stay paired, no bleed.
        for worker, claimed_session in claimed_rows:
            assert claimed_session == sessions[worker], (
                f"row claimed by {worker!r} carries session {claimed_session!r}, expected {sessions[worker]!r}"
            )
        winners = {worker for worker, _ in claimed_rows}
        assert len(winners) == _M_ROWS  # M distinct winning claimers
        winning_sessions = {sess for _, sess in claimed_rows}
        assert len(winning_sessions) == _M_ROWS  # M distinct winning sessions

    def test_pre_804_config_double_claims_over_m_rows(self, tmp_path: Path) -> None:
        """Anti-vacuity: with the pre-#804 prod config (no OPTIONS) the contract breaks.

        Unserialized ``BEGIN DEFERRED`` writers either double-claim a row (more
        than one claimer winning the same id) or collide on the write — so the
        clean "exactly M distinct, K-M None" outcome does NOT hold. Proves the
        green test guards the CAS+serialization, not a structurally-guaranteed
        post-condition.
        """
        alias = _make_alias(tmp_path, {})  # pre-#804: no serialization OPTIONS
        try:
            outcomes = _run_k_claimers(alias)
        finally:
            _teardown_alias(alias)

        claimed = [r for r in outcomes if r is not None]
        clean = len(claimed) == _M_ROWS and len(set(claimed)) == _M_ROWS
        assert not clean, f"pre-#804 config unexpectedly produced the clean contract: {outcomes}"
