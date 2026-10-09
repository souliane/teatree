"""The boot/tick recovery sweeps hold task recovery while no claim is admitted."""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LoopLease, ModeOverride, Session, Task, Ticket
from teatree.core.worktree.recovery_sweeps import run_boot_sweeps

_LAPSED = dt.timedelta(seconds=120)


def _orphaned_claim() -> Task:
    ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR)
    lapsed_at = timezone.now() - _LAPSED
    return Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, overlay="test"),
        phase="coding",
        status=Task.Status.CLAIMED,
        claimed_by="headless-worker",
        claimed_at=lapsed_at,
        heartbeat_at=lapsed_at,
        lease_expires_at=lapsed_at,
    )


def _stop_the_fleet() -> None:
    ModeOverride.objects.set_override("off", reason="test: the operator stopped the fleet")


class TestBootSweepsWaitForAnAdmittingFleet(TestCase):
    def test_a_stopped_fleet_neither_requeues_nor_fails_an_orphaned_claim(self) -> None:
        task = _orphaned_claim()
        _stop_the_fleet()

        counts = run_boot_sweeps()

        task.refresh_from_db()
        assert (counts.reclaimed_claims, counts.reaped_claims) == (0, 0)
        assert task.status == Task.Status.CLAIMED

    def test_lifting_the_stop_returns_the_orphan_to_the_queue_exactly_once(self) -> None:
        task = _orphaned_claim()
        _stop_the_fleet()
        run_boot_sweeps()
        ModeOverride.objects.all().delete()

        lifted, again = run_boot_sweeps(), run_boot_sweeps()

        task.refresh_from_db()
        assert (lifted.reclaimed_claims, again.reclaimed_claims) == (1, 0)
        assert (task.status, task.reclaim_count) == (Task.Status.PENDING, 1)

    def test_a_stopped_fleet_still_frees_a_dead_loop_lease(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="dead-session",
            owner_pid=None,
            acquired_at=now - dt.timedelta(hours=1),
            lease_expires_at=now - dt.timedelta(minutes=1),
        )
        _stop_the_fleet()

        assert run_boot_sweeps().reclaimed_leases == 1
