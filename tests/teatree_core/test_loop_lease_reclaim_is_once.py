"""A dead-owner lease is reclaimed ONCE, not re-reclaimed every tick (#3646).

After a worker replacement the loop registry still names the REPLACED worker's
pid, so every tick re-claimed its ``loop:<name>`` lease anchored on that dead
pid — and the next reclaim sweep read the live holder's own row as dead-owned,
evicted it, and logged the WARNING again at loop cadence forever. The reclaim is
now terminal per worker replacement: a provably-dead pid is never persisted as a
lease anchor, so tick two claims normally with no reclaim.
"""

import datetime as dt
from collections.abc import Callable
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopLease

_SLOT = "loop:idle_stack_reaper"
_DEAD_SESSION = "dead-session"
_LIVE_SESSION = "live-worker"
#: The replaced worker's pid, recorded in the loop registry and long since dead.
_DEAD_PID = 75882
#: The live worker's own pid.
_LIVE_PID = 4242


def _pid_alive(alive: set[int]) -> Callable[[int], bool]:
    return lambda pid: pid in alive


def _seed_dead_owner() -> None:
    now = timezone.now()
    LoopLease.objects.create(
        name=_SLOT,
        session_id=_DEAD_SESSION,
        owner_pid=_DEAD_PID,
        acquired_at=now - dt.timedelta(seconds=1800),
        lease_expires_at=now - dt.timedelta(seconds=60),
    )


def _tick(*, resolved_pid: int) -> list[str]:
    """One worker beat: the reclaim sweep, then this session's per-tick re-claim.

    ``resolved_pid`` is what the tick resolves as its durable session pid — the
    stale loop-registry value after a worker replacement.
    """
    reclaimed = LoopLease.objects.reclaim_dead_owner_leases()
    LoopLease.objects.claim_ownership(_SLOT, session_id=_LIVE_SESSION, owner_pid=resolved_pid, ttl_seconds=1800)
    return reclaimed


class TestDeadOwnerReclaimHappensOnce(TestCase):
    def test_second_tick_does_not_re_enter_the_reclaim_path(self) -> None:
        _seed_dead_owner()

        with mock.patch("teatree.utils.singleton.pid_alive", _pid_alive({_LIVE_PID})):
            first = _tick(resolved_pid=_DEAD_PID)
            second = _tick(resolved_pid=_DEAD_PID)

        assert first == [_SLOT], "the dead owner's lease must be reclaimed on the first tick"
        assert second == [], "a reclaimed lease must not be re-reclaimed on the next tick"

    def test_live_session_becomes_the_recorded_owner(self) -> None:
        _seed_dead_owner()

        with mock.patch("teatree.utils.singleton.pid_alive", _pid_alive({_LIVE_PID})):
            _tick(resolved_pid=_DEAD_PID)

        row = LoopLease.objects.get(name=_SLOT)
        assert row.session_id == _LIVE_SESSION
        assert row.owner_pid is None, "a provably-dead pid is never persisted as the lease anchor"
        assert row.lease_expires_at is not None
        assert row.lease_expires_at > timezone.now()

    def test_a_live_pid_is_still_anchored(self) -> None:
        with mock.patch("teatree.utils.singleton.pid_alive", _pid_alive({_LIVE_PID})):
            LoopLease.objects.claim_ownership(_SLOT, session_id=_LIVE_SESSION, owner_pid=_LIVE_PID, ttl_seconds=1800)

        assert LoopLease.objects.get(name=_SLOT).owner_pid == _LIVE_PID
