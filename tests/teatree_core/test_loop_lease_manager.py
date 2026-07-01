"""``LoopLeaseQuerySet.live_foreign_owner`` — the shared live-foreign-owner READ (#2777 C2).

The hook ``_db_live_foreign_owner`` reimplemented the foreign-and-live liveness
inline; this pins the manager's single predicate (which the hook now delegates
to) against the table of cases the inline version covered, and pins that the hook
delegates to it.
"""

import datetime as dt
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from django.utils import timezone

import hooks.scripts.hook_router as router
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLOT = "t3-master"


@dataclass(frozen=True)
class _Case:
    label: str
    row_session: str
    owner_pid: int | None
    expires_delta_seconds: int | None  # vs now; negative = expired; None = null expiry
    query_session: str
    current_pid: int | None
    alive_pids: frozenset[int]
    expected: str


_CASES = (
    _Case("unowned", "", None, 300, "me", 100, frozenset(), ""),
    _Case("own_session_refresh", "me", 100, 300, "me", 100, frozenset({100}), ""),
    _Case("live_foreign_diff_pid", "other", 4242, 300, "me", 100, frozenset({4242, 100}), "other"),
    _Case("expired_dead_pid", "other", 4242, -300, "me", 100, frozenset(), ""),
    _Case("same_pid_self_reclaim", "other", 100, 300, "me", 100, frozenset({100}), ""),
    _Case("null_pid_unexpired", "other", None, 300, "me", 100, frozenset(), "other"),
)


class TestLiveForeignOwner:
    @pytest.mark.parametrize("case", _CASES, ids=[c.label for c in _CASES])
    def test_equivalence_table(self, case: _Case) -> None:
        now = timezone.now()
        delta = case.expires_delta_seconds
        expires = None if delta is None else now + dt.timedelta(seconds=delta)
        LoopLease.objects.create(
            name=_SLOT, session_id=case.row_session, owner_pid=case.owner_pid, acquired_at=now, lease_expires_at=expires
        )
        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid in case.alive_pids):
            result = LoopLease.objects.live_foreign_owner(
                _SLOT, session_id=case.query_session, current_pid=case.current_pid
            )
        assert result == case.expected, case.label

    def test_missing_row_is_empty(self) -> None:
        assert LoopLease.objects.live_foreign_owner(_SLOT, session_id="me", current_pid=100) == ""


class TestHookDelegatesToManager:
    """``hook_router._db_live_foreign_owner`` is the disabled/bootstrap/fail-open envelope only.

    The foreign-and-live decision lives in the manager (mirroring the sibling
    ``_evict_stale_db_lease_owner`` which already routes through
    ``evict_stale_owner``). The hook must DELEGATE with the canonical slot + args.
    """

    def test_delegates_with_loop_owner_slot_and_args(self) -> None:
        with (
            patch.object(router, "_db_lease_consult_disabled", return_value=False),
            patch.object(router, "bootstrap_teatree_django", return_value=True),
            patch.object(LoopLease.objects, "live_foreign_owner", return_value="owner-x") as manager_call,
        ):
            result = router._db_live_foreign_owner("my-session", current_pid=4242)

        assert result == "owner-x"
        manager_call.assert_called_once_with("t3-master", session_id="my-session", current_pid=4242)

    def test_envelope_fails_open_on_manager_error(self) -> None:
        with (
            patch.object(router, "_db_lease_consult_disabled", return_value=False),
            patch.object(router, "bootstrap_teatree_django", return_value=True),
            patch.object(LoopLease.objects, "live_foreign_owner", side_effect=RuntimeError("db hiccup")),
        ):
            assert router._db_live_foreign_owner("my-session", current_pid=4242) == ""


_OWNER_PID = 4242
_OTHER_ALIVE_PID = 5555


def _seed_lease(slot: str, *, session_id: str, owner_pid: int | None, expires_delta_seconds: int) -> None:
    now = timezone.now()
    LoopLease.objects.create(
        name=slot,
        session_id=session_id,
        owner_pid=owner_pid,
        acquired_at=now,
        lease_expires_at=now + dt.timedelta(seconds=expires_delta_seconds),
    )


class TestSameProcessReclaimAcrossSessionRotation:
    """``claim_ownership`` self-heals a lease across a context-compaction session-id rotation (#2835).

    Compaction rotates ``current_session_id()`` but does NOT restart the durable
    session process, so ``current_session_pid()`` is unchanged. A live lease held
    by ``(old_session, pid)`` whose pid is the requesting caller's own pid is a
    same-process self-reclaim, not a hijack: it is RE-ANCHORED to the rotated
    session id and the claim WINS, so every native ``/loop`` keeps ticking without
    a manual ``t3 loop claim --take-over``. Cross-session mutual exclusion is
    preserved — a DIFFERENT alive pid still BLOCKS.
    """

    @pytest.mark.parametrize("slot", ["t3-master", "loop:dispatch"])
    def test_same_pid_rotation_reanchors_and_grants(self, slot: str) -> None:
        _seed_lease(slot, session_id="sessionA", owner_pid=_OWNER_PID, expires_delta_seconds=1800)
        before = LoopLease.objects.get(name=slot).lease_expires_at

        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid == _OWNER_PID):
            won, owner = LoopLease.objects.claim_ownership(
                slot, session_id="sessionB", owner_pid=_OWNER_PID, ttl_seconds=3600
            )

        assert won is True
        assert owner == "sessionB"
        row = LoopLease.objects.get(name=slot)
        assert row.session_id == "sessionB", "the rotated session id must be re-anchored onto the lease"
        assert row.owner_pid == _OWNER_PID
        assert row.lease_expires_at is not None
        assert row.lease_expires_at > before, "TTL must be refreshed"

    def test_different_alive_pid_still_blocks(self) -> None:
        _seed_lease("loop:dispatch", session_id="sessionA", owner_pid=_OWNER_PID, expires_delta_seconds=1800)

        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid in {_OWNER_PID, _OTHER_ALIVE_PID}):
            won, owner = LoopLease.objects.claim_ownership(
                "loop:dispatch", session_id="sessionB", owner_pid=_OTHER_ALIVE_PID, ttl_seconds=3600
            )

        assert won is False
        assert owner == "sessionA"
        row = LoopLease.objects.get(name="loop:dispatch")
        assert row.session_id == "sessionA", "a genuinely foreign live owner (different alive pid) must be preserved"
        assert row.owner_pid == _OWNER_PID

    def test_dead_pid_reclaim_still_works(self) -> None:
        dead_pid = 999_999
        _seed_lease("t3-master", session_id="sessionA", owner_pid=dead_pid, expires_delta_seconds=-5)

        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid == _OWNER_PID):
            won, owner = LoopLease.objects.claim_ownership(
                "t3-master", session_id="sessionB", owner_pid=_OWNER_PID, ttl_seconds=3600
            )

        assert won is True
        assert owner == "sessionB"
        row = LoopLease.objects.get(name="t3-master")
        assert row.session_id == "sessionB"
        assert row.owner_pid == _OWNER_PID

    def test_ttl_expiry_reclaim_still_works(self) -> None:
        _seed_lease("t3-master", session_id="sessionA", owner_pid=None, expires_delta_seconds=-5)

        won, owner = LoopLease.objects.claim_ownership(
            "t3-master", session_id="sessionB", owner_pid=None, ttl_seconds=3600
        )

        assert won is True
        assert owner == "sessionB"
        assert LoopLease.objects.get(name="t3-master").session_id == "sessionB"


class TestNoPingPongAcrossTicks:
    """The FIRST live master holds the per-loop lease across several ticks; the loser SKIPs every round (#2650).

    The two-session contention symptom: both sessions firing ``t3 loops tick
    --loop <name>`` made the ``loop:<name>`` lease ping-pong, so ~half the loops
    SKIPped each round. The sticky pid-anchored election keeps the FIRST live
    claimant as master across EVERY subsequent tick — a normal (``take_over=
    False``) tick from the loser never wins and never steals, and the master's
    own per-tick re-claim (the heartbeat) keeps its lease from lapsing between
    its ticks. This pins the no-ping-pong invariant the registration-side fix
    relies on: even if a loser's cron somehow fires, the lease layer holds.
    """

    def test_first_live_master_holds_across_several_ticks(self) -> None:
        slot = "loop:ship"
        alive = {_OWNER_PID, _OTHER_ALIVE_PID}
        with patch("teatree.utils.singleton.pid_alive", side_effect=lambda pid: pid in alive):
            won, owner = LoopLease.objects.claim_ownership(
                slot, session_id="master", owner_pid=_OWNER_PID, ttl_seconds=120
            )
            assert won is True
            assert owner == "master"

            for _ in range(5):
                # The loser's per-tick claim SKIPs — a normal tick never takes over a live owner.
                lost, holder = LoopLease.objects.claim_ownership(
                    slot, session_id="loser", owner_pid=_OTHER_ALIVE_PID, ttl_seconds=120
                )
                assert lost is False, "the loser must SKIP — a normal tick never takes over a live master"
                assert holder == "master"
                # The master's per-tick re-claim is the heartbeat: it refreshes and keeps mastering.
                refreshed, holder_after = LoopLease.objects.claim_ownership(
                    slot, session_id="master", owner_pid=_OWNER_PID, ttl_seconds=120
                )
                assert refreshed is True
                assert holder_after == "master"

        row = LoopLease.objects.get(name=slot)
        assert row.session_id == "master", "the lease must never ping-pong off the live first master"
        assert row.owner_pid == _OWNER_PID
