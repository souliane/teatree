"""The ``dream`` mini-loop is discoverable but off the live work loop (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute loop (issue #1933 § 3). It is its own
low-frequency cron (``t3 dream tick``) that reuses the MiniLoop cadence /
config / in-flight-lock primitives. The structural contract: the ``dream``
loop is registered (so its cadence is configured and the statusline can show
its countdown) yet excluded from the live tick (#2513 cutover — the live tick is
now the DB-``Loop``-table ``admitted_loop_names`` path; an ``off_live_tick``
row is skipped before its ``build_jobs`` runs or its ``last_run_at`` is bumped, so
the dream cron owns its ONE cadence ledger alone).
"""

import datetime as dt
import inspect
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.loop_lease_manager import LoopLeaseManager
from teatree.core.models import Loop, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.dream.loop import (
    DREAM_LEASE_SECONDS,
    DREAM_LOOP_NAME,
    DREAM_PASS_BUDGET_SECONDS,
    MINI_LOOP,
    memory_promote_enabled,
)
from teatree.loops.loop_table import admitted_loop_names
from teatree.loops.registry import iter_loops

NOW = dt.datetime(2026, 6, 11, 4, tzinfo=dt.UTC)


def _backends() -> list[OverlayBackends]:
    return [
        OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
        ),
    ]


def _context() -> dict[str, object]:
    return {
        "backends": _backends(),
        "host": None,
        "messaging": None,
        "notion_client": None,
        "ready_labels": (),
    }


class DreamMiniLoopShapeTestCase(TestCase):
    def test_loop_name_is_canonical_dream(self) -> None:
        assert MINI_LOOP.name == DREAM_LOOP_NAME == "dream"

    def test_loop_is_off_live_tick(self) -> None:
        assert MINI_LOOP.off_live_tick is True

    def test_default_cadence_is_low_frequency(self) -> None:
        # Nightly-ish: at least a day between passes (the cron drives it).
        assert MINI_LOOP.default_cadence_seconds >= 24 * 3600

    def test_build_jobs_emits_no_scanner_jobs(self) -> None:
        # The engine is invoked by the dream cron, not via the scanner-job
        # dispatch pipeline — so the MiniLoop contributes no _ScannerJob.
        assert MINI_LOOP.build_jobs(**_context()) == []


class DreamLoopRegistrationTestCase(TestCase):
    def test_dream_is_discoverable_in_registry(self) -> None:
        names = {loop.name for loop in iter_loops()}
        assert "dream" in names

    @override_settings(USE_TZ=True)
    def test_dream_emits_no_jobs_on_the_live_db_tick(self) -> None:
        # #2513 cutover: the live tick is the DB-``Loop``-table path. Dream's
        # row is daily, so on a due tick ``admitted_loop_names`` resolves it to
        # its registry ``build_jobs`` — which deliberately emits NO scanner jobs
        # (the consolidation engine runs from the dream cron, not the live tick).
        # Anti-vacuous contrast: a live mini-loop with the same row DOES emit.
        Prompt.objects.get_or_create(name="demo-dream-fk", defaults={"body": "x"})
        Loop.objects.update_or_create(
            name="dream",
            defaults={
                "script": "src/teatree/loops/dream/loop.py",
                "prompt": None,
                "delay_seconds": 86400,
                "last_run_at": None,
            },
        )
        names = admitted_loop_names(NOW, only="dream")
        assert "dream" not in names
        # The dream row's build_jobs (registry MINI_LOOP) emits nothing.
        assert MINI_LOOP.build_jobs(**_context()) == []

    @override_settings(USE_TZ=True)
    def test_dream_off_live_tick_is_not_cadence_bumped_by_master(self) -> None:
        # off_live_tick: the master must never invoke dream's build_jobs or bump its
        # last_run_at — the dream cron owns the ONE cadence ledger (Loop.last_run_at).
        # Anti-vacuous: the row is enabled, un-held, and due (interval, never run), so
        # WITHOUT the off_live_tick skip the master would bump last_run_at here.
        Loop.objects.update_or_create(
            name="dream",
            defaults={
                "script": "src/teatree/loops/dream/loop.py",
                "prompt": None,
                "delay_seconds": 86400,
                "daily_at": None,
                "enabled": True,
                "last_run_at": None,
            },
        )
        admitted_loop_names(NOW, only="dream")
        assert Loop.objects.get(name="dream").last_run_at is None


class OffLiveTickFieldTestCase(TestCase):
    def test_default_off_live_tick_is_false(self) -> None:
        loop = MiniLoop(name="x", default_cadence_seconds=60, build_jobs=lambda **_: [])
        assert loop.off_live_tick is False


class MemoryPromoteToggleTestCase(TestCase):
    """Pass-2 memory→fix promotion is default ON (#2426, #4685, #4776).

    Flipped from default OFF: the inert-rail decision (#4685 — 2133 candidates, 0
    promoted) needed the missing safety mechanism #4776 supplies (batching bounds
    the fan-out a promoting toggle can dump in one night), so turning this on by
    default can no longer flood the backlog.
    """

    def test_default_is_on_with_no_env_no_db(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("T3_DREAM_MEMORY_PROMOTE", None)
            assert memory_promote_enabled() is True

    def test_falsy_env_disables(self) -> None:
        for value in ("0", "false", "no", "off", "FALSE"):
            with patch.dict("os.environ", {"T3_DREAM_MEMORY_PROMOTE": value}):
                assert memory_promote_enabled() is False, value

    def test_truthy_env_enables(self) -> None:
        with patch.dict("os.environ", {"T3_DREAM_MEMORY_PROMOTE": "1"}):
            assert memory_promote_enabled() is True


class DreamLeaseSizingTestCase(TestCase):
    def test_lease_outlives_the_pass_budget(self) -> None:
        # A default 120s lease would expire mid-pass and let a concurrent pass
        # win the CAS. The lease must outlive the longest pass so "no two
        # overlapping passes" holds.
        assert DREAM_LEASE_SECONDS > DREAM_PASS_BUDGET_SECONDS

    def test_lease_exceeds_the_acquire_default(self) -> None:
        default_ttl = inspect.signature(LoopLeaseManager.acquire).parameters["lease_seconds"].default
        assert default_ttl < DREAM_LEASE_SECONDS
