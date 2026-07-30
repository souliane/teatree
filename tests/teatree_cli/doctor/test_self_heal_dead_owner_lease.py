"""``_check_dead_owner_lease`` — the self-repairing dead-session loop-lease watchdog (#3571).

Unlike the FAIL-only silent-freeze detectors, this one AUTO-REPAIRS: it reclaims a
``loop:<name>``/``t3-master`` lease held by a provably-dead session past TTL and reports
what it healed. Conservative + idempotent — a second run finds nothing to do.
"""

import datetime as dt
import io
from collections.abc import Callable
from contextlib import redirect_stdout

import pytest
from django.utils import timezone

from teatree.cli.doctor import self_heal
from teatree.core.models import LoopLease

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _seed_dead(slot: str) -> None:
    now = timezone.now()
    LoopLease.objects.create(
        name=slot,
        session_id="dead-session",
        owner_pid=None,
        acquired_at=now - dt.timedelta(seconds=3600),
        lease_expires_at=now - dt.timedelta(seconds=60),
    )


class DeadOwnerLeaseCheckTest:
    def test_auto_repairs_and_reports_the_heal(self) -> None:
        _seed_dead("loop:dispatch")

        ok, out = _echoes(self_heal._check_dead_owner_lease)

        assert ok is True, "an auto-healed lease is not a standing FAIL"
        assert "loop:dispatch" in out
        assert LoopLease.objects.get(name="loop:dispatch").session_id == ""

    def test_idempotent_second_run_is_silent(self) -> None:
        _seed_dead("loop:dispatch")

        _echoes(self_heal._check_dead_owner_lease)
        ok, out = _echoes(self_heal._check_dead_owner_lease)

        assert ok is True
        assert out == ""

    def test_live_owner_is_never_touched(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="live-session",
            owner_pid=None,
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(seconds=1800),
        )

        ok, out = _echoes(self_heal._check_dead_owner_lease)

        assert ok is True
        assert out == ""
        assert LoopLease.objects.get(name="loop:dispatch").session_id == "live-session"

    def test_registered_in_the_check_sequence(self) -> None:
        _seed_dead("loop:dispatch")

        assert self_heal.run_self_heal_checks() is not None
        # The sweep runs as part of the aggregate, so the dead lease is cleared.
        assert LoopLease.objects.get(name="loop:dispatch").session_id == ""
