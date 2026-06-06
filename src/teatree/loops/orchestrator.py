"""Orchestrator — fans a tick out across registered mini-loops (#1432).

Each tick walks the registry (or the injected ``registry_fn``) in
alphabetical order. For each mini-loop, the orchestrator consults
:class:`LoopsConfig` (env/per-loop/global) to decide enable/disable —
always-on loops bypass the user disable but honour the env
kill-switch. For enabled loops it asks the cadence ledger whether the
cadence has elapsed (``None`` from no marker is treated as elapsed so
a fresh install fires immediately), builds the loop's jobs (the legacy
``_ScannerJob`` shape) and passes them to ``dispatch_fn`` — the test
seam over :func:`teatree.loop.tick.run_tick`'s dispatch path. The
default ``dispatch_fn`` runs the legacy pipeline. After dispatch it
marks the loop fired and records the dispatched name on the report.
Errors in one loop's build/dispatch are caught, recorded on
``report.errors``, and never abort the orchestrator. The summary DM
builder reads the report and decides whether to fire a single
silent-when-idle DM.

The orchestrator NEVER routes by author identity — that filter stays at
scanner level (``MyPrsScanner`` skips own-author, etc.). This preserves
the reviewer-never-on-own-PR invariant via the existing scanner filters,
NOT by adding identity logic to the orchestrator.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.loops.base import BuildJobsContext, MiniLoop
from teatree.loops.cadence_ledger import MiniLoopMarker
from teatree.loops.config import LoopsConfig
from teatree.loops.gating import elapsed_and_enabled
from teatree.loops.registry import iter_loops
from teatree.loops.summary import OrchestratorReport, build_summary_dm

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
    from teatree.loop.dispatch import DispatchAction
    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.scanners.base import ScanSignal
    from teatree.loop.scanners.notion_view import NotionLike

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TickRequest:
    """Per-tick context propagated to every mini-loop's ``build_jobs``.

    Mirrors :class:`teatree.loop.tick.TickRequest` — same backend
    Protocols on every field so the orchestrator surface carries the
    same compile-time contract as the live tick path.
    """

    backends: list["OverlayBackends"] | None = None
    host: "CodeHostBackend | None" = None
    messaging: "MessagingBackend | None" = None
    notion_client: "NotionLike | None" = None
    ready_labels: tuple[str, ...] = ()


def _default_dispatch(jobs: "list[_ScannerJob]") -> "list[DispatchAction]":
    """Default dispatch — runs the legacy job pipeline.

    Imports lazily so the orchestrator stays importable from contexts
    that have not yet imported the legacy :mod:`teatree.loop`.
    """
    if not jobs:
        return []
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from teatree.loop.dispatch import dispatch  # noqa: PLC0415
    from teatree.loop.domain_jobs import _run_job  # noqa: PLC0415

    signals: list[ScanSignal] = []
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        for _label, sigs, _err in pool.map(_run_job, jobs):
            signals.extend(sigs)
    return list(dispatch(signals))


def _utc_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Orchestrator:
    """Per-domain mini-loop fan-out + summary DM (BLUEPRINT §5.6, #1432).

    Three injected seams keep the orchestrator deterministically testable:

    ``registry_fn`` (usually :func:`iter_loops`; tests pass a tuple of
    stub mini-loops), ``clock`` (usually :func:`_utc_clock`; tests pin a
    fixed datetime), and ``dispatch_fn`` (usually :func:`_default_dispatch`;
    tests record what was dispatched without running the full pipeline).
    """

    config: LoopsConfig
    registry_fn: Callable[[], tuple[MiniLoop, ...]] = field(default=iter_loops)
    clock: Callable[[], dt.datetime] = field(default=_utc_clock)
    dispatch_fn: "Callable[[list[_ScannerJob]], list[DispatchAction]]" = field(default=_default_dispatch)

    def tick(self, request: TickRequest) -> "TickOutcome":
        """Run one orchestrator tick — gate, dispatch, summarise."""
        started_at = self.clock()
        dispatched: list[str] = []
        skipped: dict[str, str] = {}
        errors: dict[str, str] = {}
        actions_total = 0

        kwargs = _kwargs_for_request(request)
        for loop in self.registry_fn():
            decision = elapsed_and_enabled(self.config, loop, started_at)
            if not decision.should_fire:
                skipped[loop.name] = decision.skip_reason or ""
                continue
            try:
                jobs = loop.build_jobs(**kwargs)
                actions = self.dispatch_fn(jobs)
            except Exception as exc:
                logger.exception("mini-loop %r raised", loop.name)
                errors[loop.name] = f"{type(exc).__name__}: {exc}"
                continue
            dispatched.append(loop.name)
            actions_total += len(actions)
            MiniLoopMarker.objects.mark_fired(loop.name, started_at)

        final_report = OrchestratorReport(
            signals_count=actions_total,
            actions_count=actions_total,
            errors=errors,
            dispatched_loops=dispatched,
            skipped_loops=skipped,
        )
        self._maybe_send_summary(final_report, started_at)
        return TickOutcome(
            started_at=started_at,
            dispatched_loops=dispatched,
            skipped_loops=skipped,
            errors=errors,
            actions_count=actions_total,
        )

    def _maybe_send_summary(self, report: OrchestratorReport, started_at: dt.datetime) -> None:
        """Build and send the summary DM honouring the configured policy."""
        utc_day = started_at.date().isoformat()
        dm = build_summary_dm(
            report,
            policy=self.config.summary_dm,
            utc_day=utc_day,
            tick_id=started_at.isoformat(),
        )
        if dm is None:
            return
        try:
            self._notify(dm.text, idempotency_key=dm.idempotency_key)
        except Exception:
            logger.exception("loop-summary DM send failed")

    @staticmethod
    def _notify(text: str, *, idempotency_key: str) -> None:
        """Send the summary DM. Lazy import so tests can monkeypatch easily."""
        from teatree.messaging import notify_with_fallback  # noqa: PLC0415
        from teatree.notify import NotifyKind  # noqa: PLC0415

        notify_with_fallback(text, kind=NotifyKind.INFO, idempotency_key=idempotency_key)


def _kwargs_for_request(request: TickRequest) -> BuildJobsContext:
    return {
        "backends": request.backends,
        "host": request.host,
        "messaging": request.messaging,
        "notion_client": request.notion_client,
        "ready_labels": request.ready_labels,
    }


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """Result of one orchestrator tick — for callers and tests."""

    started_at: dt.datetime
    dispatched_loops: list[str]
    skipped_loops: dict[str, str]
    errors: dict[str, str]
    actions_count: int
