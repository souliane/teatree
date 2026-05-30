"""Live tick fans out through the mini-loop registry (#1481).

``teatree.loops.fanout.build_registry_jobs`` is the single source of
which scanners run a tick. It applies the same enable + cadence gate the
orchestrator uses (:func:`teatree.loops.gating.elapsed_and_enabled`), so
the live tick and the orchestrator never drift. The ``loop_tick``
command injects it into ``run_tick`` via the ``jobs_builder`` seam.
"""

import datetime as dt
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.backends.protocols import CodeHostBackend
from teatree.core.backend_factory import OverlayBackends
from teatree.core.management.commands.loop_tick import _registry_jobs_builder
from teatree.core.models import MiniLoopMarker
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickRequest, run_tick
from teatree.loops.config import LoopOverride, LoopsConfig
from teatree.loops.fanout import build_registry_jobs
from teatree.loops.orchestrator import Orchestrator
from teatree.loops.orchestrator import TickRequest as OrchestratorTickRequest
from teatree.loops.registry import iter_loops

NOW = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)


@dataclass(slots=True)
class _FixedScanner:
    name: str = "fixed"
    out: list[ScanSignal] = field(default_factory=lambda: [ScanSignal(kind="my_pr.open", summary="x")])

    def scan(self) -> list[ScanSignal]:
        return self.out


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


def _job_set(jobs: list[object]) -> set[tuple[str, str]]:
    return {(job.scanner.name, job.overlay) for job in jobs}


class BuildRegistryJobsTestCase(TestCase):
    def test_resource_pressure_scanner_still_wired(self) -> None:
        jobs = build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)
        assert "resource_pressure" in {job.scanner.name for job in jobs}

    def test_per_loop_disabled_drops_its_scanners(self) -> None:
        config = LoopsConfig(per_loop={"tickets": LoopOverride(enabled=False)})
        names = {job.scanner.name for job in build_registry_jobs(_context(), config=config, now=NOW)}
        assert "stale_tickets" not in names
        assert "active_tickets" not in names
        assert "pending_tasks" in names

    def test_env_kill_switch_drops_named_loop(self) -> None:
        old = os.environ.get("T3_LOOPS_DISABLED")
        try:
            os.environ["T3_LOOPS_DISABLED"] = "tickets"
            names = {job.scanner.name for job in build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)}
            assert "stale_tickets" not in names
            assert "pending_tasks" in names
        finally:
            if old is None:
                os.environ.pop("T3_LOOPS_DISABLED", None)
            else:
                os.environ["T3_LOOPS_DISABLED"] = old

    def test_cadence_not_elapsed_drops_its_scanners(self) -> None:
        MiniLoopMarker.objects.mark_fired("tickets", NOW - dt.timedelta(seconds=1))
        names = {job.scanner.name for job in build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)}
        assert "stale_tickets" not in names

    def test_marks_fired_loops(self) -> None:
        build_registry_jobs(_context(), config=LoopsConfig(), now=NOW)
        assert MiniLoopMarker.objects.filter(name="tickets").exists()
        assert MiniLoopMarker.objects.filter(name="dispatch").exists()

    def test_matches_orchestrator_job_set_for_same_inputs(self) -> None:
        backends = _backends()
        captured: list[object] = []

        Orchestrator(
            config=LoopsConfig(),
            registry_fn=iter_loops,
            clock=lambda: NOW,
            dispatch_fn=lambda jobs: captured.extend(jobs) or list(jobs),
        ).tick(OrchestratorTickRequest(backends=backends))

        MiniLoopMarker.objects.all().delete()
        live = build_registry_jobs(
            {"backends": backends, "host": None, "messaging": None, "notion_client": None, "ready_labels": ()},
            config=LoopsConfig(),
            now=NOW,
        )
        assert _job_set(live) == _job_set(captured)


class RunTickRegistrySeamTestCase(TestCase):
    def test_run_tick_uses_injected_jobs_builder_and_keeps_side_effects(self) -> None:
        from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

        seen: list[tuple[object, dt.datetime]] = []

        def _builder(request: TickRequest, started_at: dt.datetime) -> list[_ScannerJob]:
            seen.append((request, started_at))
            return [_ScannerJob(scanner=_FixedScanner(), overlay="")]

        with tempfile.TemporaryDirectory() as tmp:
            statusline = Path(tmp) / "statusline.txt"
            report = run_tick(
                TickRequest(backends=_backends()),
                statusline_path=statusline,
                now=NOW,
                jobs_builder=_builder,
            )
            assert len(seen) == 1
            assert seen[0][1] == NOW
            assert report.signal_count == 1
            assert statusline.is_file()
            assert (Path(tmp) / "tick-meta.json").is_file()

    def test_loop_tick_builder_marks_loops_fired(self) -> None:
        _registry_jobs_builder(TickRequest(backends=_backends()), NOW)
        assert MiniLoopMarker.objects.filter(name="dispatch").exists()
