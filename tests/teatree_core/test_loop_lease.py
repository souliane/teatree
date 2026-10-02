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
from pathlib import Path

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core import loop_lease_manager
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

    def test_same_owner_reacquire_extends_via_cas(self) -> None:
        # acquire()'s Q(owner=owner) CAS branch — re-acquire by the same
        # owner extends the lease. (Not LoopLease.renew(), which #54
        # removed as dead — the per-tick acquire IS the renewal.)
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

    def test_release_preserves_the_last_acquired_anchor(self) -> None:
        """A release clears the CLAIM, never the record that the slot ran.

        ``acquired_at`` is the live claim and is nulled on release, so it cannot
        answer "when did this slot last run?" — every cleanly-released infra slot
        read as never-fired forever. ``last_acquired_at`` is the durable anchor
        the status surfaces read instead.
        """
        assert LoopLease.objects.acquire("loop-slack-answer", owner="pid-1") is True
        fired_at = LoopLease.objects.get(name="loop-slack-answer").last_acquired_at
        assert fired_at is not None

        assert LoopLease.objects.release("loop-slack-answer", owner="pid-1") is True
        lease = LoopLease.objects.get(name="loop-slack-answer")
        assert lease.acquired_at is None, "the live claim is cleared on release"
        assert lease.last_acquired_at == fired_at, "the fire anchor survives the release"

    def test_reacquire_advances_the_last_acquired_anchor(self) -> None:
        LoopLease.objects.acquire("loop-slack-answer", owner="pid-1")
        first = LoopLease.objects.get(name="loop-slack-answer").last_acquired_at
        LoopLease.objects.release("loop-slack-answer", owner="pid-1")

        LoopLease.objects.acquire("loop-slack-answer", owner="pid-2")
        second = LoopLease.objects.get(name="loop-slack-answer").last_acquired_at
        assert second is not None
        assert first is not None
        assert second > first

    def test_release_only_by_holder(self) -> None:
        LoopLease.objects.acquire("loop-tick", owner="pid-1")
        assert LoopLease.objects.release("loop-tick", owner="someone-else") is False
        assert LoopLease.objects.get(name="loop-tick").owner == "pid-1"
        assert LoopLease.objects.release("loop-tick", owner="pid-1") is True
        assert LoopLease.objects.get(name="loop-tick").owner == ""


class TestFireAnchorSurvivesEveryReleasePath(TestCase):
    """Every acquisition stamps the anchor and no release clears it.

    Scoped to the CLASS, not the one call site that produced the ticket: the slot
    status surfaces read one column, so an acquisition path that forgets to stamp
    it — or a release path that nulls it — re-creates "this loop has never fired"
    for whichever slot uses that path.
    """

    def test_claim_ownership_stamps_the_anchor(self) -> None:
        won, _ = LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        assert won is True
        assert LoopLease.objects.get(name="t3-master").last_acquired_at is not None

    def test_release_ownership_preserves_the_anchor(self) -> None:
        LoopLease.objects.claim_ownership("t3-master", session_id="sess-A")
        anchored = LoopLease.objects.get(name="t3-master").last_acquired_at

        assert LoopLease.objects.release_ownership("t3-master", session_id="sess-A") is True

        lease = LoopLease.objects.get(name="t3-master")
        assert lease.acquired_at is None
        assert lease.last_acquired_at == anchored

    def test_every_acquisition_write_stamps_the_anchor(self) -> None:
        """No ``acquired_at=now`` write may land without its ``last_acquired_at`` twin.

        A source-level parity check over the manager, because an acquisition path
        added later would otherwise pass every behavioural test above while
        silently regressing the surface those tests exist to protect.
        """
        source = Path(loop_lease_manager.__file__).read_text(encoding="utf-8")
        anchored = source.count("last_acquired_at=now,")
        # ``last_acquired_at=now,`` contains ``acquired_at=now,``, so the plain
        # claim writes are the difference between the two counts.
        claims = source.count("acquired_at=now,") - anchored
        assert claims == anchored, "every `acquired_at=now` write must be paired with `last_acquired_at=now`"


class TestLoopLeaseModelSurface(TestCase):
    """#54 (WS2 follow-up): pin the trimmed model surface + coverage gaps.

    F1/F2: dead ``renew()`` and write-never-read ``heartbeat_at`` are
    removed. Plus the previously-uncovered ``__str__`` and the
    ``is_held`` unowned / None-expiry early-return branch.
    """

    def test_str_owned(self) -> None:
        LoopLease.objects.acquire("loop-tick", owner="pid-7")
        lease = LoopLease.objects.get(name="loop-tick")
        assert str(lease) == "loop-lease<loop-tick owner=pid-7>"

    def test_str_unowned_uses_dash(self) -> None:
        lease = LoopLease.objects.create(name="dormant")
        assert str(lease) == "loop-lease<dormant owner=->"

    def test_is_held_false_when_unowned(self) -> None:
        # Early-return branch: no owner -> not held (even if a stale
        # future expiry somehow lingers).
        lease = LoopLease.objects.create(
            name="x",
            owner="",
            lease_expires_at=timezone.now() + timedelta(seconds=999),
        )
        assert lease.is_held is False

    def test_is_held_false_when_expiry_is_none(self) -> None:
        lease = LoopLease.objects.create(name="y", owner="pid-1", lease_expires_at=None)
        assert lease.is_held is False

    def test_is_held_false_when_expired(self) -> None:
        lease = LoopLease.objects.create(
            name="z",
            owner="pid-1",
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        assert lease.is_held is False

    def test_renew_method_is_removed(self) -> None:
        # #54 F2: dead method (no production caller) is gone.
        assert not hasattr(LoopLease, "renew")

    def test_heartbeat_at_field_is_removed(self) -> None:
        # #54 F1: write-never-read column dropped until a later WS needs it.
        field_names = {f.name for f in LoopLease._meta.get_fields()}
        assert "heartbeat_at" not in field_names


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
