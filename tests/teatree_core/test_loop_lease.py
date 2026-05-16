"""Tests for the DB loop-ownership lease (#786 WS2).

Keystone is the SQLite-prod-backend anti-vacuous concurrency test: the
production DB is SQLite (``has_select_for_update_skip_locked`` is
``False``), so the lease's mutual exclusion must come from a conditional
``UPDATE`` compare-and-swap, NOT row locking. The race test reproduces
the actual double-owner outcome via a deterministic write-boundary
interleave (the #786 B1 lesson — a sequential or Postgres-only test is
vacuous for concurrency).
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopLease


class TestLoopLeaseAcquireRelease(TestCase):
    def test_acquire_unowned_lease_succeeds_and_is_held(self) -> None:
        assert LoopLease.objects.acquire("loop-tick", owner="pid-1") is True
        lease = LoopLease.objects.get(name="loop-tick")
        assert lease.owner == "pid-1"
        assert lease.is_held is True

    def test_second_owner_cannot_acquire_live_lease(self) -> None:
        assert LoopLease.objects.acquire("loop-tick", owner="pid-1") is True
        assert LoopLease.objects.acquire("loop-tick", owner="pid-2") is False
        assert LoopLease.objects.get(name="loop-tick").owner == "pid-1"

    def test_same_owner_reacquire_renews(self) -> None:
        assert LoopLease.objects.acquire("loop-tick", owner="pid-1") is True
        assert LoopLease.objects.acquire("loop-tick", owner="pid-1") is True
        assert LoopLease.objects.get(name="loop-tick").owner == "pid-1"

    def test_expired_lease_is_reclaimable_by_new_owner(self) -> None:
        assert LoopLease.objects.acquire("loop-tick", owner="dead-pid", lease_seconds=1) is True
        lease = LoopLease.objects.get(name="loop-tick")
        lease.lease_expires_at = timezone.now() - timedelta(seconds=5)
        lease.save(update_fields=["lease_expires_at"])

        assert LoopLease.objects.acquire("loop-tick", owner="successor") is True
        assert LoopLease.objects.get(name="loop-tick").owner == "successor"

    def test_release_only_by_holder(self) -> None:
        LoopLease.objects.acquire("loop-tick", owner="pid-1")
        assert LoopLease.objects.release("loop-tick", owner="someone-else") is False
        assert LoopLease.objects.get(name="loop-tick").owner == "pid-1"
        assert LoopLease.objects.release("loop-tick", owner="pid-1") is True
        assert LoopLease.objects.get(name="loop-tick").owner == ""


class TestLoopLeaseConcurrencyOnSqlite(TestCase):
    """#786 WS2 keystone — single-owner guaranteed on the production SQLite backend.

    SQLite has ``has_select_for_update_skip_locked = False``; the lease's
    mutual exclusion is a conditional ``UPDATE ... WHERE (unowned OR
    expired)`` compare-and-swap. This reproduces a *concurrent interleave*
    (two ticks both past the unowned-read before either writes), NOT a
    sequential check. RED without the CAS guard (both ticks acquire →
    two live loop owners → double-dispatch); GREEN with it (exactly one
    wins). Proven RED→GREEN by reverting the production hunk in this
    change.
    """

    def test_backend_is_sqlite(self) -> None:
        assert connection.vendor == "sqlite"
        assert connection.features.has_select_for_update_skip_locked is False

    def test_interleaved_ticks_acquire_lease_exactly_once(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from django.db.models import QuerySet  # noqa: PLC0415

        # Seed the row so both ticks see the same unowned lease.
        LoopLease.objects.get_or_create(name="loop-tick")

        fired: list[str] = []
        rival_result: list[object] = [None]
        real_update = QuerySet.update

        def update_with_rival(self: object, *args: object, **kwargs: object) -> object:
            # Injected just before tick-1's conditional UPDATE commits: a
            # concurrent tick-2 runs its full acquire (its own CAS) inside
            # tick-1's critical section. Fire exactly once.
            if not fired:
                fired.append("x")
                rival_result[0] = LoopLease.objects.acquire("loop-tick", owner="tick-2")
            return real_update(self, *args, **kwargs)

        with patch.object(QuerySet, "update", update_with_rival):
            tick1 = LoopLease.objects.acquire("loop-tick", owner="tick-1")

        tick2 = rival_result[0]
        winners = [o for (o, won) in (("tick-1", tick1), ("tick-2", tick2)) if won]
        # Exactly ONE tick acquired the lease — never two live owners.
        assert len(winners) == 1, f"double-owner race NOT closed on SQLite: {tick1=} {tick2=}"
        lease = LoopLease.objects.get(name="loop-tick")
        assert lease.owner == winners[0]
        assert lease.is_held is True
