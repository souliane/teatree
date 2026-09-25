"""Concurrent ``_claim_or_reclaim`` grants the durable claim once (#1103).

The #1103 fix splits the live-scan dedup head from the durable claim and
makes a *stale* unposted orphan reclaimable. The race-safety guarantee
the durable claim exists for must still hold: under a clean (empty) live
scan, two concurrent callers racing the claim on the same canonical MR
URL must yield **exactly one** ``post`` and one ``already_claimed``,
with exactly one row — a *recent* concurrent claim is NOT a stale orphan
and must not be reclaimed.

On Django's SQLite backend ``select_for_update`` is a documented no-op
(#804); serialization comes from the connection's ``BEGIN`` mode — prod
sets ``transaction_mode="IMMEDIATE"`` (``SQLITE_WRITE_SERIALIZATION_OPTIONS``)
so the first writer takes SQLite's reserved write lock at transaction
start and the second blocks on the busy_timeout, then reads the row
already present and returns ``already_claimed``. This mirrors
``test_review_request_post_concurrent.py`` / ``test_on_behalf_approval_concurrent.py``
verbatim (``_make_alias`` + ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` +
``threading.Barrier``); it models the REAL ``_claim_or_reclaim``
primitive (``using``-threaded), not an ``atomic()`` approximation.

Anti-vacuity: drop ``transaction_mode`` → ``{}`` AND remove the
``select_for_update()`` from ``_claim_or_reclaim`` — the test goes RED
(both callers ``get_or_create(created=True)`` → two ``post`` decisions,
the lost-update: two duplicate review requests would go out). Restoring
either makes it GREEN — it pins the locking + write-mode contract.
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from django.test import override_settings

from teatree.core.gates.review_request_guard import GuardTarget, _claim_or_reclaim, canonical_mr_url
from teatree.core.models import ReviewRequestPost
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from tests.db_alias import RouteAllToAlias, register_sqlite_alias, run_racing_threads, teardown_sqlite_alias

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_TARGET = GuardTarget(channel_id="C0DEMOCHAN1", channel_name="the-review-team", token="xoxb-bot")


def _make_alias(tmp_path: Path) -> str:
    """Register a Django connection against a fresh file-backed SQLite DB.

    Matches prod's ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` so a concurrent
    second claim hits ``BEGIN IMMEDIATE`` and blocks on the busy_timeout
    instead of reading the row free under a no-op ``select_for_update``.
    """
    alias = f"rrg_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    register_sqlite_alias(alias, db_file, options=SQLITE_WRITE_SERIALIZATION_OPTIONS)
    with override_settings(DATABASE_ROUTERS=[RouteAllToAlias(alias)]):
        call_command("migrate", "--no-input", database=alias, verbosity=0)
    connections[alias].close()
    return alias


def _run_two_claims(alias: str, canonical: str) -> list[str]:
    """Two real threads race ``_claim_or_reclaim`` on the same MR URL."""
    barrier = threading.Barrier(2)

    def claim(_idx: int) -> str:
        barrier.wait(timeout=10)
        return _claim_or_reclaim(canonical, _TARGET, "overlay-a", using=alias).action

    return run_racing_threads(claim, 2)


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
class TestReviewRequestClaimConcurrent:
    """Two concurrent claims on the same MR grant the durable claim once."""

    def test_concurrent_claims_grant_one_post(self, tmp_path: Path) -> None:
        canonical = canonical_mr_url(_MR_URL)
        alias = _make_alias(tmp_path)
        try:
            assert ("core", "0102_review_request_post_overlay") in MigrationRecorder(
                connections[alias]
            ).applied_migrations()
            outcomes = _run_two_claims(alias, canonical)
            rows = ReviewRequestPost.objects.using(alias).count()
        finally:
            teardown_sqlite_alias(alias)

        posts = [o for o in outcomes if o == "post"]
        suppressed = [o for o in outcomes if o == "suppress"]
        assert len(posts) == 1, f"expected exactly one claim to win (post), got {outcomes!r}"
        assert len(suppressed) == 1, f"expected exactly one to stand down (suppress), got {outcomes!r}"
        assert rows == 1, f"expected exactly one durable row, got {rows}"
