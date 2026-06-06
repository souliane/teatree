"""Live tick fans out through the mini-loop registry (#1481).

``teatree.loops.fanout.build_registry_jobs`` is the single source of
which scanners run a tick. It applies the same enable + cadence gate the
orchestrator uses (:func:`teatree.loops.gating.elapsed_and_enabled`), so
the live tick and the orchestrator never drift. The ``loop_tick``
command injects it into ``run_tick`` via the ``jobs_builder`` seam.
"""

import dataclasses
import datetime as dt
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.core.management.commands.loop_tick import _registry_jobs_builder
from teatree.core.models import MiniLoopMarker
from teatree.loop.global_scanner_factories import build_default_jobs
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


def _context(backends: list[OverlayBackends] | None = None) -> dict[str, object]:
    return {
        "backends": backends if backends is not None else _backends(),
        "host": None,
        "messaging": None,
        "notion_client": None,
        "ready_labels": (),
    }


def _job_set(jobs: list[object]) -> set[tuple[str, str]]:
    return {(job.scanner.name, job.overlay) for job in jobs}


def _arg_value(value: object) -> object:
    if isinstance(value, MagicMock):
        return id(value)
    if isinstance(value, (list, tuple)):
        return tuple(_arg_value(item) for item in value)
    if isinstance(value, (str, int, float, bool, bytes, type(None))):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            tuple((f.name, _arg_value(getattr(value, f.name))) for f in dataclasses.fields(value)),
        )
    # Helper instances (repliers, api clients, classifiers) without value
    # equality compare by type — two equivalent constructions are equal.
    return type(value).__name__


def _scanner_signature(job: Any) -> tuple[Any, ...]:
    scanner = job.scanner
    fields = sorted(f.name for f in dataclasses.fields(scanner)) if dataclasses.is_dataclass(scanner) else []
    args = tuple((name, _arg_value(getattr(scanner, name))) for name in fields)
    return (type(scanner).__name__, getattr(scanner, "name", ""), job.overlay, args)


def _signature_multiset(jobs: list[object]) -> list[tuple[Any, ...]]:
    return sorted((_scanner_signature(j) for j in jobs), key=repr)


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
        live = build_registry_jobs(_context(backends), config=LoopsConfig(), now=NOW)
        assert _job_set(live) == _job_set(captured)


class RegistryLegacyParityTestCase(TestCase):
    """The registry fan-out is behaviour-equal to the legacy ``build_default_jobs``.

    On a fresh cadence ledger every loop is eligible, so the registry sum
    must reproduce the exact scanner set — identity *and* args — the
    legacy monolithic fan-out produced. This guard is what makes
    scanner-set drift (double-emit, missing args, dropped scanner) a
    failing test rather than a silent regression.
    """

    @staticmethod
    def _production_backend() -> OverlayBackends:
        host = MagicMock(spec=CodeHostBackend)
        messaging = MagicMock(spec=MessagingBackend)
        return OverlayBackends(
            name="teatree",
            hosts=(host,),
            messaging=messaging,
            ready_labels=("ready",),
        )

    def test_registry_matches_legacy_scanner_signatures(self) -> None:
        backends = [self._production_backend()]
        legacy = build_default_jobs(backends=backends)
        MiniLoopMarker.objects.all().delete()
        registry = build_registry_jobs(_context(backends), config=LoopsConfig(), now=NOW)
        assert _signature_multiset(registry) == _signature_multiset(legacy)

    def test_review_nag_emitted_exactly_once(self) -> None:
        backends = [self._production_backend()]
        registry = build_registry_jobs(_context(backends), config=LoopsConfig(), now=NOW)
        nags = [j for j in registry if j.scanner.name == "review_nag"]
        legacy_nags = [j for j in build_default_jobs(backends=backends) if j.scanner.name == "review_nag"]
        assert len(legacy_nags) == 1
        assert len(nags) == 1

    def test_default_off_issue_implementer_keeps_parity_and_emits_nothing(self) -> None:
        """The default-OFF ``ISSUE_IMPLEMENTER`` domain leaves both fan-out paths unchanged (#1553).

        With ``issue_implementer_enabled`` defaulting to False the per-overlay
        slice is empty, so neither the registry path nor the legacy path emits
        the scanner — the byte-for-byte parity guard stays green even though the
        new domain is a partition member.
        """
        backends = [self._production_backend()]
        legacy = build_default_jobs(backends=backends)
        MiniLoopMarker.objects.all().delete()
        registry = build_registry_jobs(_context(backends), config=LoopsConfig(), now=NOW)
        assert _signature_multiset(registry) == _signature_multiset(legacy)
        assert "issue_implementer" not in {j.scanner.name for j in legacy}
        assert "issue_implementer" not in {j.scanner.name for j in registry}

    def test_my_prs_and_reviewer_prs_carry_url_attribution(self) -> None:
        """The ship/review domain slices pass the same non-empty URL-attribution the legacy fan-out does.

        Uses a real ``GitHubCodeHost`` (offline) so ``_web_origin_for_host``
        resolves and a workspace-repo overlay so ``allowed_url_prefixes`` is
        non-empty — otherwise omitting the kwargs is indistinguishable from
        passing the empty default and the guard would be vacuous.
        """
        from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
        from teatree.loop.job_identity import Domain  # noqa: PLC0415
        from teatree.loop.scanner_factories import _jobs_for_backend_hosts  # noqa: PLC0415

        backend = self._url_gated_backend()
        legacy = {
            (j.scanner.name, j.overlay): j.scanner
            for j in _jobs_for_backend_hosts(backend, backend.name, all_backends=(backend,))
        }
        ship = jobs_for_domain(Domain.SHIP, backend, all_backends=(backend,))
        review = jobs_for_domain(Domain.REVIEW, backend, all_backends=(backend,))

        my_prs = next(j.scanner for j in ship if j.scanner.name == "my_prs")
        reviewer = next(j.scanner for j in review if j.scanner.name == "reviewer_prs")

        assert my_prs.allowed_url_prefixes == ("https://github.com/owner/repo/",)
        assert my_prs.allowed_url_prefixes == legacy["my_prs", backend.name].allowed_url_prefixes
        assert my_prs.competing_url_prefixes == legacy["my_prs", backend.name].competing_url_prefixes
        assert reviewer.allowed_url_prefixes == legacy["reviewer_prs", backend.name].allowed_url_prefixes
        assert reviewer.competing_url_prefixes == legacy["reviewer_prs", backend.name].competing_url_prefixes

    @staticmethod
    def _url_gated_backend() -> OverlayBackends:
        from teatree.backends.github import GitHubCodeHost  # noqa: PLC0415

        overlay = MagicMock()
        overlay.get_workspace_repos.return_value = ["owner/repo"]
        return OverlayBackends(
            name="teatree",
            hosts=(GitHubCodeHost(token=""),),
            messaging=None,
            ready_labels=(),
            overlay=overlay,
        )


class RunTickRegistrySeamTestCase(TestCase):
    def test_run_tick_uses_injected_jobs_builder_and_keeps_side_effects(self) -> None:
        from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415

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
