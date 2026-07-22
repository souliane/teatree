"""``run_boot_sweeps`` reclaims dead-session loop leases so ``t3 recover`` unblocks them (#3571).

Before #3571 the boot sweep reclaimed only ``Task`` rows; a ``loop:<name>`` lease held by
a dead session was invisible to it, so ``t3 recover`` never cleared the frozen loop.
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models import LoopLease
from teatree.core.worktree.recovery_sweeps import run_boot_sweeps

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestBootSweepReclaimsDeadLeases:
    def test_boot_sweep_reclaims_dead_session_lease(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="dead-session",
            owner_pid=None,
            acquired_at=now - dt.timedelta(seconds=3600),
            lease_expires_at=now - dt.timedelta(seconds=60),
        )

        counts = run_boot_sweeps()

        assert counts.reclaimed_leases == 1
        assert LoopLease.objects.get(name="loop:dispatch").session_id == ""

    def test_boot_sweep_keeps_live_owner_lease(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="live-session",
            owner_pid=None,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(seconds=1800),
        )

        counts = run_boot_sweeps()

        assert counts.reclaimed_leases == 0
        assert LoopLease.objects.get(name="loop:dispatch").session_id == "live-session"
