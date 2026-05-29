"""Concurrent ``review_request_post`` consumes the #960 approval once (#1098).

The sanctioned post command rides the same load-bearing single-use
guarantee as every other on-behalf publish path: its #960
``require_on_behalf_approval`` step calls ``OnBehalfApproval.consume``,
which serializes two concurrent posts on the same
``(canonical_mr_url, "review_request_post")`` via ``select_for_update``
inside ``transaction.atomic`` so the second consumer observes the first's
``consumed_at`` before its own UPDATE commits. On Django's SQLite backend
``select_for_update`` is a documented no-op (#804); serialization comes
from the connection's ``BEGIN`` mode — prod sets
``transaction_mode="IMMEDIATE"`` (``SQLITE_WRITE_SERIALIZATION_OPTIONS``)
so the first writer takes SQLite's reserved write lock at transaction
start and the second blocks on the busy_timeout, then reads the row
already consumed and returns ``None`` (the second post stands down — it
must NOT post a duplicate review request on a replayed approval).

This is the post-command scoping of ``test_on_behalf_approval_concurrent.py``:
the contended resource is exactly the approval the command consumes, the
target is the canonical MR URL, the action is ``review_request_post``.

Anti-vacuity: temporarily change ``consume`` to a plain ``filter()``
instead of ``select_for_update`` and revert ``transaction_mode`` to
unconfigured (``{}``) — the test goes RED (both posts consume the same
approval, the lost-update — two duplicate review requests would go out).
Restoring either the locking statement or the prod ``OPTIONS`` makes it
GREEN — it pins the locking + write-mode contract, not just one.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db import connections

from teatree.core.models import OnBehalfApproval
from teatree.core.models.on_behalf_approval import canonical_on_behalf_target
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_ACTION = "review_request_post"


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB.

    Matches prod's ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` so a concurrent
    second post hits ``BEGIN IMMEDIATE`` and blocks on the busy_timeout
    instead of reading the approval free under a no-op ``select_for_update``.
    """
    alias = f"rrp_{uuid.uuid4().hex}"
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
    # Only ``teatree_on_behalf_approval`` is touched by the consume codepath
    # under test (the audit row is FK-only here); the minimal schema suffices
    # and avoids replaying the #541 data migration against the empty default.
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
    connections[alias].close()
    return alias


def _teardown_alias(alias: str) -> None:
    for conn in connections.all():
        if conn.alias == alias:
            conn.close()
    connections.databases.pop(alias, None)


def _run_two_posts(alias: str, target: str, action: str) -> list[OnBehalfApproval | None]:
    """Two real threads race the post command's approval-consume step."""
    barrier = threading.Barrier(2)
    results: dict[int, OnBehalfApproval | None] = {}

    def runner(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[idx] = OnBehalfApproval.consume(target, action, using=alias)
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
    """Lift pytest-django's DB-access guard for the whole test.

    This module spins its own private file-backed SQLite connection on
    ``tmp_path`` and tears it down itself; pytest-django's guard would
    otherwise reject the runtime-registered alias.
    """
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestReviewRequestPostConcurrentApproval:
    """Two concurrent posts on the same MR consume the #960 approval once."""

    def test_concurrent_posts_consume_one_approval(self, tmp_path: Path) -> None:
        canonical = canonical_on_behalf_target(_MR_URL)
        alias = _make_alias(tmp_path)
        try:
            OnBehalfApproval.objects.using(alias).create(
                target=canonical,
                action=_ACTION,
                approver_id="souliane",
            )
            outcomes = _run_two_posts(alias, canonical, _ACTION)
        finally:
            _teardown_alias(alias)

        winners = [o for o in outcomes if o is not None]
        losers = [o for o in outcomes if o is None]
        assert len(winners) == 1, f"expected exactly one post to win the approval, got {outcomes!r}"
        assert len(losers) == 1, f"expected exactly one post to stand down (None), got {outcomes!r}"
        assert winners[0].consumed_at is not None

    def test_only_one_audit_after_concurrent_posts(self, tmp_path: Path) -> None:
        """The losing post consumes nothing, so the command writes ZERO extra audits.

        Mirrors what the command does after each consume: write an audit
        IFF consume returned non-None — guards the audit-cardinality
        invariant the on-behalf channel relies on under a concurrent post.
        """
        from teatree.core.models import OnBehalfAudit  # noqa: PLC0415

        canonical = canonical_on_behalf_target(_MR_URL)
        alias = _make_alias(tmp_path)
        try:
            OnBehalfApproval.objects.using(alias).create(
                target=canonical,
                action=_ACTION,
                approver_id="souliane",
            )
            outcomes = _run_two_posts(alias, canonical, _ACTION)
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
