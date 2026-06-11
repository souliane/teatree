"""The ``dream`` mini-loop is discoverable but off the live work loop (#1933).

The dreaming consolidation pass is heavier than a scanner tick and must not
run on — or re-arm — the live 12-minute loop (issue #1933 § 3). It is its own
low-frequency cron (``t3 dream tick``) that reuses the MiniLoop cadence /
config / in-flight-lock primitives. The structural contract: the ``dream``
loop is registered (so its cadence is configured under ``[loops.dream]`` and
the statusline can show its countdown) yet excluded from both the live-tick
fan-out (``build_registry_jobs``) and the orchestrator's normal dispatch.
"""

import datetime as dt
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.dream.loop import DREAM_LOOP_NAME, MINI_LOOP
from teatree.loops.fanout import build_registry_jobs
from teatree.loops.orchestrator import Orchestrator
from teatree.loops.orchestrator import TickRequest as OrchestratorTickRequest
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

    def test_dream_excluded_from_live_tick_fanout(self) -> None:
        MiniLoopMarker.objects.all().delete()
        jobs = build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)
        assert all(job.overlay != "dream" for job in jobs)
        # An off-live-tick loop must NOT be marked fired by the live tick —
        # its cadence ledger is owned by its own cron.
        assert not MiniLoopMarker.objects.filter(name="dream").exists()

    def test_dream_excluded_from_orchestrator_dispatch(self) -> None:
        MiniLoopMarker.objects.all().delete()
        captured: list[object] = []

        def _dispatch(jobs: list[object]) -> list[object]:
            captured.extend(jobs)
            return list(jobs)

        outcome = Orchestrator(
            config=LoopsConfig(),
            registry_fn=iter_loops,
            clock=lambda: NOW,
            dispatch_fn=_dispatch,
        ).tick(OrchestratorTickRequest(backends=_backends()))
        # The off-live-tick loop is skipped before build/dispatch, so its
        # marker is never bumped by the orchestrator and it is not dispatched.
        assert not MiniLoopMarker.objects.filter(name="dream").exists()
        assert "dream" not in outcome.dispatched_loops
        assert outcome.skipped_loops.get("dream") == "off_live_tick"


class OffLiveTickFieldTestCase(TestCase):
    def test_default_off_live_tick_is_false(self) -> None:
        loop = MiniLoop(name="x", default_cadence_seconds=60, build_jobs=lambda **_: [])
        assert loop.off_live_tick is False
