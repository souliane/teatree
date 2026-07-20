"""teatree.loops.statusline_refresh — the headless statusline-render chain (#256 stale-line).

The crux behavioural contract: the pre-rendered ``statusline.txt`` is re-rendered on a
short cadence WITHOUT any domain loop being admitted-and-ticking, so the loop line never
freezes headless — but only when it has actually aged (a fresh file is left alone, no
flicker) and only while the ``autoload`` #256 flag is ON (a colleague box renders nothing).
Integration-first against the real DB + ``django_tasks_db`` backend and a real temp XDG
home, so the render truly writes the file and refreshes its freshness sidecar.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import django.test
from django.utils import timezone

from teatree.core.models import LoopLease
from teatree.core.models.config_setting import ConfigSetting
from teatree.loop.statusline_staleness import FLOOR_SECONDS
from teatree.loops import statusline_refresh, timer_reconciler
from teatree.loops.statusline_refresh import (
    REFRESH_AGE_SECONDS,
    STATUSLINE_RENDER_LEASE,
    ensure_statusline_refresh_chain,
    refresh_statusline_if_due,
    render_statusline,
)

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}


def _set_autoload(*, on: bool) -> None:
    ConfigSetting.objects.set_value("autoload", value=on, scope="")


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class _RefreshCase(django.test.TestCase):
    """Base: an isolated temp XDG home so ``default_path()`` writes under the test dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.statusline = Path(self._tmp.name) / "teatree" / "statusline.txt"
        self.meta = self.statusline.with_name("tick-meta.json")

    def _write_meta(self, *, rendered_at: float) -> None:
        self.meta.parent.mkdir(parents=True, exist_ok=True)
        self.meta.write_text(json.dumps({"rendered_at": rendered_at}), encoding="utf-8")

    def _meta_rendered_at(self) -> float:
        return json.loads(self.meta.read_text(encoding="utf-8"))["rendered_at"]


class TestAutoloadGate(_RefreshCase):
    """The #256 colleague guarantee: autoload OFF renders nothing, no matter how stale."""

    def test_gated_off_renders_nothing(self) -> None:
        _set_autoload(on=False)
        now = timezone.now()
        self._write_meta(rendered_at=now.timestamp() - 10 * FLOOR_SECONDS)  # hours stale

        outcome = refresh_statusline_if_due(now)

        assert outcome == "gated"
        assert not self.statusline.exists()


class TestFreshnessGuard(_RefreshCase):
    """Renders only once the file has aged past the refresh threshold — a fresh file is left alone."""

    def test_renders_when_never_rendered(self) -> None:
        _set_autoload(on=True)
        assert not self.meta.exists()

        outcome = refresh_statusline_if_due(timezone.now())

        assert outcome == "rendered"
        assert self.statusline.exists()  # bootstrapped from a live render
        assert self.meta.exists()

    def test_skips_when_file_is_fresh(self) -> None:
        _set_autoload(on=True)
        now = timezone.now()
        self._write_meta(rendered_at=now.timestamp() - (REFRESH_AGE_SECONDS - 5))  # just under the threshold
        self.statusline.write_text("PRIOR TICK CONTENT\n", encoding="utf-8")

        outcome = refresh_statusline_if_due(now)

        assert outcome == "fresh"
        # A fresh file (a recent per-loop tick already rendered it) is NOT re-rendered,
        # so this chain never blanks that tick's scanned zones.
        assert self.statusline.read_text(encoding="utf-8") == "PRIOR TICK CONTENT\n"

    def test_renders_when_file_is_stale(self) -> None:
        _set_autoload(on=True)
        now = timezone.now()
        stale_at = now.timestamp() - (REFRESH_AGE_SECONDS + 30)
        self._write_meta(rendered_at=stale_at)

        outcome = refresh_statusline_if_due(now)

        assert outcome == "rendered"
        assert self.statusline.exists()
        # The freshness sidecar advanced in lock-step so the stale-banner probes agree.
        assert self._meta_rendered_at() > stale_at

    def test_refresh_threshold_is_well_under_the_stale_cutoff(self) -> None:
        # Keeps the file fresh long before the readers would flag it STALE (floor 300s),
        # yet above the shortest 60s per-loop cadence so a healthy fleet stays dormant.
        assert 60 < REFRESH_AGE_SECONDS < FLOOR_SECONDS


class TestLeaseSerialisation(_RefreshCase):
    """Two concurrent fires never both install the process-global reader seams and race teardown."""

    def test_contended_lease_skips_render(self) -> None:
        _set_autoload(on=True)
        now = timezone.now()
        self._write_meta(rendered_at=now.timestamp() - (REFRESH_AGE_SECONDS + 30))
        assert LoopLease.objects.acquire(STATUSLINE_RENDER_LEASE, owner="other-holder")

        outcome = refresh_statusline_if_due(now)

        assert outcome == "contended"
        assert not self.statusline.exists()

    def test_lease_released_after_render(self) -> None:
        _set_autoload(on=True)
        refresh_statusline_if_due(timezone.now())
        # Released in the finally, so a subsequent owner can take it.
        assert LoopLease.objects.acquire(STATUSLINE_RENDER_LEASE, owner="next-owner")


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestChain(django.test.TestCase):
    """The self-rescheduling chain contract — mirrors the reconcile/prune maintenance chains."""

    def setUp(self) -> None:
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — test-local heavy dep

        DBTaskResult.objects.all().delete()

    def _pending(self) -> int:
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — test-local heavy dep
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — test-local heavy dep

        return DBTaskResult.objects.filter(
            task_path=render_statusline.module_path, status=TaskResultStatus.READY
        ).count()

    def test_fire_reschedules_itself(self) -> None:
        _set_autoload(on=False)  # gated body keeps the fire cheap; the re-arm still happens
        result = render_statusline.func()
        assert result["action"] != "deduped"
        assert self._pending() == 1  # a successor render chain is queued

    def test_fire_self_dedups(self) -> None:
        render_statusline.using(run_after=timezone.now()).enqueue()
        result = render_statusline.func()
        assert result == {"action": "deduped"}

    def test_ensure_seeds_head_once_and_is_idempotent(self) -> None:
        ensure_statusline_refresh_chain()
        assert self._pending() == 1
        ensure_statusline_refresh_chain()
        assert self._pending() == 1  # idempotent — no duplicate head

    def test_worker_startup_maintenance_seed_arms_the_chain(self) -> None:
        # A deploy/restart re-arms it: ensure_maintenance_chains seeds the render chain
        # alongside its sibling maintenance chains.
        timer_reconciler.ensure_maintenance_chains()
        assert self._pending() == 1
        assert statusline_refresh.render_statusline.module_path  # sanity: the task is importable
