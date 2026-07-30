"""Dead-session per-loop lease reclaim — the ``pid_alive``-forever trap (#3571).

A ``loop:<name>`` lease anchored on a NON-null ``owner_pid`` was kept live forever
whenever ``pid_alive(owner_pid)`` returned ``True`` — but a dead owner session's pid
is routinely REUSED (or belongs to a DIFFERENT container namespace), so ``pid_alive``
lies and the live worker SKIPs the loop indefinitely. The fix: for a per-loop slot the
alive-pid verdict no longer overrides an EXPIRED TTL — the per-tick re-claim IS the
owning session's heartbeat, so a lapsed TTL means the session stopped driving the loop
and the lease is reclaimable. The global ``t3-master`` slot keeps its #1604 pid-anchored
busy-owner protection (a live pid past TTL stays), so this must not regress it.
"""

import datetime as dt
import logging

import pytest
from django.utils import timezone

from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_PER_LOOP_SLOT = "loop:dispatch"
_MASTER_SLOT = "t3-master"
_DEAD_SESSION = "dead-session"
_LIVE_WORKER_SESSION = "live-worker"
#: The dead owner's pid, still "alive" because it was reused by an unrelated process.
_REUSED_PID = 4242
#: The live worker's own distinct pid.
_WORKER_PID = 100


def _seed(slot: str, *, session_id: str, owner_pid: int | None, expires_delta_seconds: int) -> None:
    now = timezone.now()
    LoopLease.objects.create(
        name=slot,
        session_id=session_id,
        owner_pid=owner_pid,
        acquired_at=now - dt.timedelta(seconds=1800),
        lease_expires_at=now + dt.timedelta(seconds=expires_delta_seconds),
    )


def _pid_alive(alive: set[int]):
    return lambda pid: pid in alive


class TestPerLoopDeadSessionReusedPid:
    """A per-loop lease with a REUSED-but-alive pid past TTL is reclaimable (the bug)."""

    def test_live_worker_claims_dead_session_lease(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=_REUSED_PID, expires_delta_seconds=-60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive({_REUSED_PID, _WORKER_PID}))
            won, owner = LoopLease.objects.claim_ownership(
                _PER_LOOP_SLOT, session_id=_LIVE_WORKER_SESSION, owner_pid=_WORKER_PID, ttl_seconds=1800
            )

        assert won is True, "a live worker must reclaim a per-loop lease whose owning session is dead"
        assert owner == _LIVE_WORKER_SESSION
        assert LoopLease.objects.get(name=_PER_LOOP_SLOT).session_id == _LIVE_WORKER_SESSION

    def test_reclaim_sweep_orphans_it(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=_REUSED_PID, expires_delta_seconds=-60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive({_REUSED_PID}))
            reclaimed = LoopLease.objects.reclaim_dead_owner_leases()

        assert reclaimed == [_PER_LOOP_SLOT]
        row = LoopLease.objects.get(name=_PER_LOOP_SLOT)
        assert row.session_id == ""
        assert row.owner_pid is None


class TestPerLoopLiveOwnerNotStolen:
    """A genuinely live owner (fresh heartbeat / fresh TTL) is never stolen — the #3534 guard."""

    def test_fresh_heartbeat_blocks_takeover(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=_REUSED_PID, expires_delta_seconds=1800)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive({_REUSED_PID, _WORKER_PID}))
            won, owner = LoopLease.objects.claim_ownership(
                _PER_LOOP_SLOT, session_id=_LIVE_WORKER_SESSION, owner_pid=_WORKER_PID, ttl_seconds=1800
            )

        assert won is False, "a fresh-heartbeat live owner must never be stolen (duplicate-run hazard)"
        assert owner == _DEAD_SESSION
        assert LoopLease.objects.get(name=_PER_LOOP_SLOT).session_id == _DEAD_SESSION

    def test_reclaim_sweep_keeps_live_owner(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id="live-owner", owner_pid=_REUSED_PID, expires_delta_seconds=1800)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive({_REUSED_PID}))
            reclaimed = LoopLease.objects.reclaim_dead_owner_leases()

        assert reclaimed == []
        assert LoopLease.objects.get(name=_PER_LOOP_SLOT).session_id == "live-owner"


class TestPerLoopIndeterminateOwnerTTL:
    """A null-pid (indeterminate) owner falls back to the TTL: reclaimed once expired, kept while fresh."""

    def test_expired_null_pid_is_reclaimed(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=None, expires_delta_seconds=-60)

        reclaimed = LoopLease.objects.reclaim_dead_owner_leases()

        assert reclaimed == [_PER_LOOP_SLOT]
        assert LoopLease.objects.get(name=_PER_LOOP_SLOT).session_id == ""

    def test_fresh_null_pid_is_kept(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=None, expires_delta_seconds=1800)

        reclaimed = LoopLease.objects.reclaim_dead_owner_leases()

        assert reclaimed == []
        assert LoopLease.objects.get(name=_PER_LOOP_SLOT).session_id == _DEAD_SESSION


class TestMasterSlotBusyOwnerPreserved:
    """The global ``t3-master`` slot keeps its #1604 busy-owner-past-TTL protection (no regression)."""

    def test_alive_pid_past_ttl_is_not_reclaimed(self) -> None:
        _seed(_MASTER_SLOT, session_id="master", owner_pid=_REUSED_PID, expires_delta_seconds=-60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive({_REUSED_PID}))
            reclaimed = LoopLease.objects.reclaim_dead_owner_leases()
            won, owner = LoopLease.objects.claim_ownership(
                _MASTER_SLOT, session_id="fresh", owner_pid=_WORKER_PID, ttl_seconds=1800
            )

        assert reclaimed == [], "a busy t3-master owner (alive pid) must be preserved past its TTL"
        assert won is False
        assert owner == "master"

    def test_dead_pid_past_ttl_is_reclaimed(self) -> None:
        _seed(_MASTER_SLOT, session_id="master", owner_pid=_REUSED_PID, expires_delta_seconds=-60)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("teatree.utils.singleton.pid_alive", _pid_alive(set()))
            reclaimed = LoopLease.objects.reclaim_dead_owner_leases()

        assert reclaimed == [_MASTER_SLOT], "a provably-dead t3-master owner is still reclaimed"


class TestReclaimSweepProperties:
    """Idempotence + loud logging of every eviction."""

    def test_second_sweep_is_a_noop(self) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=None, expires_delta_seconds=-60)

        first = LoopLease.objects.reclaim_dead_owner_leases()
        second = LoopLease.objects.reclaim_dead_owner_leases()

        assert first == [_PER_LOOP_SLOT]
        assert second == []

    def test_eviction_logs_loudly(self, caplog: pytest.LogCaptureFixture) -> None:
        _seed(_PER_LOOP_SLOT, session_id=_DEAD_SESSION, owner_pid=None, expires_delta_seconds=-60)

        with caplog.at_level(logging.WARNING, logger="teatree.core.loop_lease_manager"):
            LoopLease.objects.reclaim_dead_owner_leases()

        assert any(_PER_LOOP_SLOT in rec.getMessage() and _DEAD_SESSION in rec.getMessage() for rec in caplog.records)

    def test_unowned_rows_are_never_touched(self) -> None:
        LoopLease.objects.create(name="loop-tick", owner="", session_id="")

        assert LoopLease.objects.reclaim_dead_owner_leases() == []
