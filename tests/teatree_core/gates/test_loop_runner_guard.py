"""Tests for the loop-tick-runner doctor gate (resource_pressure RETRO).

The disk-full incident root cause: ``t3 loop status`` showed scanners configured
with intervals (``resource_pressure`` 1m, …) but ``crontab -l`` had NO entry
firing ``t3 loop tick`` — so NO scanner ever ran (the intervals are config, not
a live scheduler). The gap was *silent*: nothing surfaced "configured but never
ticking". This gate makes it loud.

The predicate (``loop_runner_health``): enabled ``Loop`` rows are *configured*
work; a live tick runner is what *drives* them. A runner is live when either a
``loop-owner`` session lease is live (a session pumps the tick) OR the
``loop-tick`` lease shows a recent acquisition (a cron just ticked). Enabled
rows + no live runner ⇒ unhealthy (the incident state). No enabled rows ⇒
healthy (the PAUSED-by-default install drives nothing on purpose).
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.gates.loop_runner_guard import doctor_check_loop_tick_runner, loop_runner_health
from teatree.core.models import Loop, LoopLease, Prompt

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _enable_a_loop(name: str = "resource_pressure") -> Loop:
    return Loop.objects.create(name=name, script=f"loops/{name}/loop.py", delay_seconds=60, enabled=True)


def _live_owner_lease(now: dt.datetime) -> LoopLease:
    import os  # noqa: PLC0415

    return LoopLease.objects.create(
        name="loop-owner",
        owner="sess-1",
        session_id="sess-1",
        owner_pid=os.getpid(),  # our own pid is alive
        acquired_at=now,
        lease_expires_at=now + dt.timedelta(seconds=1800),
    )


def _recent_tick_lease(now: dt.datetime) -> LoopLease:
    return LoopLease.objects.create(
        name="loop-tick",
        owner="pid-123",
        acquired_at=now - dt.timedelta(seconds=30),
        lease_expires_at=now + dt.timedelta(seconds=30),
    )


class LoopRunnerHealthTests(TestCase):
    """``loop_runner_health`` distinguishes 'configured but not ticking' from healthy."""

    def test_enabled_loops_with_no_runner_is_unhealthy(self) -> None:
        """The incident state: an operator enabled a loop, but nothing ticks it."""
        _enable_a_loop()
        health = loop_runner_health(now=timezone.now())
        assert health.healthy is False
        assert health.enabled_loop_count == 1
        assert "resource_pressure" in health.enabled_loop_names

    def test_no_enabled_loops_is_healthy(self) -> None:
        """The PAUSED-by-default install drives nothing on purpose — not a defect."""
        Loop.objects.create(name="paused", script="loops/paused/loop.py", delay_seconds=60, enabled=False)
        health = loop_runner_health(now=timezone.now())
        assert health.healthy is True
        assert health.enabled_loop_count == 0

    def test_empty_loop_table_is_healthy(self) -> None:
        health = loop_runner_health(now=timezone.now())
        assert health.healthy is True

    def test_live_owner_session_means_healthy(self) -> None:
        """A live ``loop-owner`` session pumps the tick — the runner is alive."""
        now = timezone.now()
        _enable_a_loop()
        _live_owner_lease(now)
        health = loop_runner_health(now=now)
        assert health.healthy is True

    def test_recent_tick_lease_means_healthy(self) -> None:
        """A cron that just acquired ``loop-tick`` proves the runner is alive."""
        now = timezone.now()
        _enable_a_loop()
        _recent_tick_lease(now)
        health = loop_runner_health(now=now)
        assert health.healthy is True

    def test_stale_tick_lease_with_no_owner_is_unhealthy(self) -> None:
        """A ``loop-tick`` lease last acquired hours ago, no live owner ⇒ stalled."""
        now = timezone.now()
        _enable_a_loop()
        LoopLease.objects.create(
            name="loop-tick",
            owner="pid-999",
            acquired_at=now - dt.timedelta(hours=6),
            lease_expires_at=now - dt.timedelta(hours=5),
        )
        health = loop_runner_health(now=now)
        assert health.healthy is False
        assert health.last_tick_age_seconds is not None
        assert health.last_tick_age_seconds > 3600

    def test_dead_owner_pid_with_no_recent_tick_is_unhealthy(self) -> None:
        """An owner lease whose pid is dead and TTL lapsed is not a live runner."""
        now = timezone.now()
        _enable_a_loop()
        LoopLease.objects.create(
            name="loop-owner",
            owner="sess-dead",
            session_id="sess-dead",
            owner_pid=2_000_000_000,  # implausible pid → not alive
            acquired_at=now - dt.timedelta(hours=2),
            lease_expires_at=now - dt.timedelta(hours=1),  # TTL lapsed
        )
        health = loop_runner_health(now=now)
        assert health.healthy is False

    def test_prompt_backed_enabled_loop_counts(self) -> None:
        """Enabled loops are counted regardless of prompt-vs-script backing."""
        prompt = Prompt.objects.create(name="p", body="do the work")
        Loop.objects.create(name="arch_review", prompt=prompt, delay_seconds=86400, enabled=True)
        health = loop_runner_health(now=timezone.now())
        assert health.healthy is False
        assert "arch_review" in health.enabled_loop_names


class DoctorSurfaceTests(TestCase):
    """``doctor_check_loop_tick_runner`` is the ``t3 doctor`` FAIL surface."""

    def test_fails_and_emits_remediation_when_configured_but_not_ticking(self) -> None:
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415

        _enable_a_loop()
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = doctor_check_loop_tick_runner()
        out = buf.getvalue()
        assert ok is False
        assert "FAIL" in out
        assert "resource_pressure" in out
        assert "t3 loop tick" in out  # names the remediation

    def test_passes_silently_when_nothing_enabled(self) -> None:
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415

        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = doctor_check_loop_tick_runner()
        assert ok is True
        assert buf.getvalue() == ""

    def test_crash_proof_degrades_to_warn_and_passes(self) -> None:
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.gates import loop_runner_guard  # noqa: PLC0415

        buf = io.StringIO()
        with (
            redirect_stdout(buf),
            patch.object(loop_runner_guard, "loop_runner_health", side_effect=RuntimeError("db")),
        ):
            ok = doctor_check_loop_tick_runner()
        assert ok is True  # a DB error must never red the whole doctor run
        assert "WARN" in buf.getvalue()
