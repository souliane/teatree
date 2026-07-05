"""Tick-driver column on the ``LoopLease`` (PR-26 / M9).

Ownership (a live ``session_id``) says WHO may run a loop; the ``driver`` says
WHAT fires its ticks. The load-bearing subtlety is the per-tick heartbeat
re-claim: a same-holder refresh whose detection momentarily returns blank must
PRESERVE the stored driver (``_driver_after`` preserve-on-empty), while a holder
change installs the incoming value verbatim — including blank, so a new holder
never inherits a dead owner's driver label. Both are observed RED against a
naive ``driver=driver`` overwrite before the ``_driver_after`` guard.
"""

import os
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopDriver, LoopLease

_SLOT = "t3-master-driver-test"


def _driver(slot: str) -> str:
    return LoopLease.objects.filter(name=slot).values_list("driver", flat=True).first() or ""


class TestDriverDefault(TestCase):
    def test_fresh_lease_row_has_blank_driver(self) -> None:
        LoopLease.objects.get_or_create(name=_SLOT)
        assert _driver(_SLOT) == ""

    def test_first_claim_writes_the_driver(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        assert _driver(_SLOT) == "self_pump"

    def test_claim_with_no_driver_stays_blank_driverless(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid())
        assert _driver(_SLOT) == ""


class TestHeartbeatPreservesDriver(TestCase):
    """The observed-RED anti-vacuity core: a heartbeat re-claim never wipes the driver."""

    def test_same_holder_heartbeat_with_blank_detection_preserves_driver(self) -> None:
        # Register a driver, then re-claim (the per-tick heartbeat) with a blank
        # incoming driver — the exact shape of a tick whose detection momentarily
        # fails (DB hiccup / unreadable registry). The registration must survive.
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver="")
        assert won is True
        assert _driver(_SLOT) == "self_pump"

    def test_same_holder_heartbeat_with_new_driver_overwrites(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER)
        assert _driver(_SLOT) == "loop_runner"

    def test_compaction_self_reclaim_with_blank_preserves_driver(self) -> None:
        # #2835 shape: same OS process, rotated session id, blank incoming driver.
        # The re-anchor to the rotated id is not a transfer, so the driver holds.
        LoopLease.objects.claim_ownership(
            _SLOT, session_id="old-id", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER
        )
        won, current = LoopLease.objects.claim_ownership(
            _SLOT, session_id="rotated-id", owner_pid=os.getpid(), driver=""
        )
        assert won is True
        assert current == "rotated-id"
        assert _driver(_SLOT) == "loop_runner"


class TestHolderChangeWritesVerbatim(TestCase):
    """A NEW holder installs its own reality — it never inherits the dead owner's driver."""

    def _expire_foreign(self, session_id: str, driver: str) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id=session_id, owner_pid=None, driver=driver)
        LoopLease.objects.filter(name=_SLOT).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

    def test_failover_reclaim_installs_the_new_driver(self) -> None:
        self._expire_foreign("dead", LoopDriver.SELF_PUMP)
        won, _ = LoopLease.objects.claim_ownership(
            _SLOT, session_id="new", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER
        )
        assert won is True
        assert _driver(_SLOT) == "loop_runner"

    def test_failover_reclaim_with_blank_driver_does_not_inherit_stale_label(self) -> None:
        # The stale-driver-after-holder-change guard: a new holder claiming with a
        # blank driver is genuinely driverless and must NOT keep the dead owner's label.
        self._expire_foreign("dead", LoopDriver.SELF_PUMP)
        won, _ = LoopLease.objects.claim_ownership(_SLOT, session_id="new", owner_pid=os.getpid(), driver="")
        assert won is True
        assert _driver(_SLOT) == ""

    def test_take_over_by_different_session_installs_new_driver(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        LoopLease.objects.take_over_ownership(
            _SLOT, session_id="b", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER
        )
        assert _driver(_SLOT) == "loop_runner"

    def test_take_over_by_different_session_with_blank_driver_blanks_it(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        LoopLease.objects.take_over_ownership(_SLOT, session_id="b", owner_pid=os.getpid(), driver="")
        assert _driver(_SLOT) == ""


class TestExactlyOneDriverPerLoop(TestCase):
    def test_racing_claim_on_live_slot_keeps_the_winners_driver(self) -> None:
        # The CAS: with a's pid alive, b's claim of the same live slot is blocked,
        # so the row carries exactly the winner's driver and b learns who holds it.
        won_a, _ = LoopLease.objects.claim_ownership(
            _SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP
        )
        won_b, owner = LoopLease.objects.claim_ownership(
            _SLOT, session_id="b", owner_pid=os.getpid() + 1, driver=LoopDriver.LOOP_RUNNER
        )
        assert won_a is True
        assert won_b is False
        assert owner == "a"
        assert _driver(_SLOT) == "self_pump"

    def test_two_per_loop_slots_carry_independent_drivers(self) -> None:
        LoopLease.objects.claim_ownership(
            "loop:dispatch", session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP
        )
        LoopLease.objects.claim_ownership(
            "loop:tickets", session_id="a", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER
        )
        assert _driver("loop:dispatch") == "self_pump"
        assert _driver("loop:tickets") == "loop_runner"


class TestReleaseAndStatus(TestCase):
    def test_release_clears_the_driver(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.SELF_PUMP)
        LoopLease.objects.release_ownership(_SLOT, session_id="a")
        assert LoopLease.objects.ownership_status(_SLOT).driver == ""

    def test_ownership_status_surfaces_the_driver(self) -> None:
        LoopLease.objects.claim_ownership(_SLOT, session_id="a", owner_pid=os.getpid(), driver=LoopDriver.LOOP_RUNNER)
        assert LoopLease.objects.ownership_status(_SLOT).driver == "loop_runner"

    def test_ownership_status_of_missing_slot_has_blank_driver(self) -> None:
        assert LoopLease.objects.ownership_status("never-claimed").driver == ""
