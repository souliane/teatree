"""Concurrent ``BotPing.claim_delivery`` grants the DM to exactly one tick.

``notify_user``'s double-DM TOCTOU was a bare ``filter → first → delete``
pre-delivery guard: two concurrent ticks both passed it, both delivered, and
the user was double-DM'd. The fix is the atomic ``BotPing.claim_delivery``
CAS — a ``select_for_update`` re-read inside one ``transaction.atomic`` — so
only one tick wins the right to deliver. This pins that single-grant
guarantee under real two-thread contention.

On Django's SQLite backend ``select_for_update`` is a documented no-op
(#804); serialization comes from the connection's ``BEGIN`` mode. Prod sets
``transaction_mode="IMMEDIATE"`` (``SQLITE_WRITE_SERIALIZATION_OPTIONS``) so
the first claimer takes SQLite's reserved write lock at transaction start and
the second blocks on the busy_timeout, then re-reads the row already
``SENDING`` and stands down. ``notify_user`` itself hardcodes the ``default``
connection (which the test runner pins to an in-memory DB), so — exactly like
``test_on_behalf_approval_concurrent.py`` exercises ``OnBehalfApproval.consume``
— this races the claim primitive on a private file-backed alias.

Anti-vacuity: drop the ``select_for_update`` + ``transaction.atomic`` from
``claim_delivery`` (bare ``filter`` + create) and revert ``transaction_mode``
to ``{}`` and the test goes RED — both threads return ``CLAIMED`` (the
double-grant). Restoring either the locking statement or the prod ``OPTIONS``
makes it GREEN: exactly one ``CLAIMED``, one ``IN_FLIGHT``.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import connections

from teatree.core.models import BotPing, DeliveryClaim
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS

_KEY = "concurrent-claim-key"


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB with prod OPTIONS."""
    alias = f"notify_{uuid.uuid4().hex}"
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
        cur.execute(
            """
            CREATE TABLE teatree_bot_ping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key VARCHAR(255) NOT NULL UNIQUE,
                kind VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL,
                text TEXT NOT NULL,
                audience VARCHAR(32) NOT NULL DEFAULT '',
                channel_ref VARCHAR(255) NOT NULL DEFAULT '',
                posted_ts VARCHAR(64) NOT NULL DEFAULT '',
                permalink VARCHAR(512) NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                transport VARCHAR(16) NOT NULL DEFAULT '',
                attempts INTEGER UNSIGNED NOT NULL DEFAULT 0,
                posted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def _run_two_claims(alias: str) -> list[DeliveryClaim | None]:
    barrier = threading.Barrier(2)
    results: dict[int, DeliveryClaim] = {}

    def runner(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = BotPing.claim_delivery(_KEY, kind="info", text="tests are green", using=alias)
        finally:
            connections[alias].close()

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return [results.get(0), results.get(1)]


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard — this test owns its private alias."""
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestClaimDeliveryConcurrent:
    """Two real threads race ``claim_delivery`` on a file-backed SQLite with IMMEDIATE."""

    def test_clean_slate_grants_exactly_one_winner(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            outcomes = _run_two_claims(alias)
            sending = BotPing.objects.using(alias).filter(idempotency_key=_KEY, status=BotPing.Status.SENDING).count()
            total = BotPing.objects.using(alias).filter(idempotency_key=_KEY).count()
        finally:
            _teardown_alias(alias)

        claimed = [o for o in outcomes if o == DeliveryClaim.CLAIMED]
        in_flight = [o for o in outcomes if o == DeliveryClaim.IN_FLIGHT]
        assert len(claimed) == 1, f"expected exactly one delivery grant, got {outcomes!r}"
        assert len(in_flight) == 1, f"expected the loser to see IN_FLIGHT, got {outcomes!r}"
        assert sending == 1, f"expected one SENDING claim row, got {sending}"
        assert total == 1, f"expected one BotPing row for the key, got {total}"

    def test_recoverable_prior_row_grants_exactly_one_winner(self, tmp_path: Path) -> None:
        """A prior FAILED row both ticks see — only one replaces it with a fresh claim."""
        alias = _make_alias(tmp_path)
        try:
            BotPing.objects.using(alias).create(
                idempotency_key=_KEY,
                kind=BotPing.Kind.INFO.value,
                status=BotPing.Status.FAILED,
                text="first attempt failed",
            )
            outcomes = _run_two_claims(alias)
            total = BotPing.objects.using(alias).filter(idempotency_key=_KEY).count()
        finally:
            _teardown_alias(alias)

        claimed = [o for o in outcomes if o == DeliveryClaim.CLAIMED]
        in_flight = [o for o in outcomes if o == DeliveryClaim.IN_FLIGHT]
        assert len(claimed) == 1, f"expected exactly one delivery grant, got {outcomes!r}"
        assert len(in_flight) == 1, f"expected the loser to see IN_FLIGHT, got {outcomes!r}"
        assert total == 1, f"expected one BotPing row for the key, got {total}"

    def test_already_sent_row_never_grants_redelivery(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            BotPing.objects.using(alias).create(
                idempotency_key=_KEY,
                kind=BotPing.Kind.INFO.value,
                status=BotPing.Status.SENT,
                text="already delivered",
            )
            outcomes = _run_two_claims(alias)
        finally:
            _teardown_alias(alias)

        assert all(o == DeliveryClaim.ALREADY_SENT for o in outcomes), (
            f"a SENT row must never grant redelivery, got {outcomes!r}"
        )
