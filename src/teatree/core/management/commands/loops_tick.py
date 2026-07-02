"""``manage.py loops_tick`` — one PER-LOOP tick of a single DB ``Loop`` row (#2650).

The single tick surface for ``t3 loops tick --loop <name>``: the per-loop
primitive each native Claude ``/loop`` fires. **There is NO master tick.** The
loop is PER-LOOP ONLY — one native Claude ``/loop`` per enabled ``Loop`` row, each
firing this command scoped to its own row on its own cadence. Invoking
``t3 loops tick`` with NO ``--loop`` is a hard error pointing at the per-loop
usage, so neither a human nor an agent can start a fat fan-out tick.

Each per-loop tick first reconciles availability (#2544): both drivers that fire
this command — the loop-runner daemon's ``execute_loop`` task
(``call_command("loops_tick", loop=name)``) and the legacy native Claude
``/loop`` cron (which runs ``t3 loops tick --loop <name>``) — converge here, so
consulting :func:`teatree.core.availability.resolve_mode` in ONE place reconciles
both drivers identically. When the resolved mode's ``pauses_self_pump`` is true
(holiday-``away`` only), the tick is skipped silently (parked) before any lease
is claimed or overlay is preflighted; ``autonomous_away`` defers questions like
``away`` but does NOT pause here, so an unattended run keeps self-pumping.

Otherwise the tick scopes the jobs builder to that ONE enabled, due row, claims
the disjoint per-loop ``loop:<name>`` lease (so the N per-loop loops run in
parallel instead of serialising on a single owner) plus the ``loop-tick:<name>``
mutex, preflights ONLY that loop's overlay (so one overlay's connector outage
cannot fail an unrelated loop's tick, LOOP-PR-C), re-anchors any deferred
self-update reinstall in this fresh subprocess before scanner imports, installs
the mini-loop schedules reader so the statusline loop line keeps its per-loop
countdowns, and runs the shared :func:`teatree.loop.tick.run_tick` pipeline (reap
+ scan + act + render).

The reactive infra loops (Slack-answer, self-improve, drain-queue) are NOT driven
here: each is its own dedicated native Claude ``/loop`` running its own
``t3 loop <slot> run`` command on its own cadence (``teatree.cli.loop*``), behind
its own dedicated ``LoopLease``.
"""

import datetime as dt
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.core import availability
from teatree.core.backend_factory import code_host_from_overlay, iter_overlay_backends, messaging_from_overlay
from teatree.core.loop_lease_manager import PER_LOOP_TICK_MUTEX_PREFIX, per_loop_owner_slot
from teatree.core.models import LoopLease
from teatree.loop.loop_cadences import loop_owner_ttl_seconds

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.tick import TickReport, TickRequest
    from teatree.loops.base import BuildJobsContext

type ReportDict = dict[str, Any]

#: The dedicated lease slot the reinstall drain acquires so parallel per-loop
#: ticks never both re-anchor the same pending reinstall; its short TTL doubles as
#: a throttle (a re-tick inside the window loses the CAS and skips) — the
#: CAS-as-throttle shape (claim-if-stale, never released).
_REINSTALL_DRAIN_SLOT = "loop-reinstall"
_REINSTALL_DRAIN_THROTTLE_SECONDS = 60


def _scanner_context(request: "TickRequest") -> "BuildJobsContext":
    return {
        "backends": request.backends,
        "host": request.host,
        "messaging": request.messaging,
        "notion_client": request.notion_client,
        "ready_labels": request.ready_labels,
    }


def _scoped_jobs_builder(only: str) -> "Callable[[TickRequest, dt.datetime], list[_ScannerJob]]":
    """A jobs builder scoped to the ONE enabled loop ``t3 loops tick --loop`` fires (#2650).

    Returns a closure that scopes :func:`build_loop_table_jobs` to that single
    row, so the per-loop ``/loop`` runs exactly its own loop and every other row
    is untouched (its cadence anchor unconsumed).
    """

    def builder(request: "TickRequest", started_at: dt.datetime) -> "list[_ScannerJob]":
        from teatree.loops.loop_table import build_loop_table_jobs  # noqa: PLC0415

        return build_loop_table_jobs(_scanner_context(request), now=started_at, only=only)

    return builder


def _report_to_dict(report: "TickReport") -> ReportDict:
    return {
        "started_at": report.started_at.isoformat(),
        "signal_count": report.signal_count,
        "action_count": report.action_count,
        "statusline_path": str(report.statusline_path) if report.statusline_path else "",
        "errors": report.errors,
        "actions": [asdict(action) for action in report.actions],
        "skipped": False,
        "skipped_reason": "",
    }


def _skipped_report_dict(started_at: dt.datetime, reason: str) -> ReportDict:
    """The full report contract for a tick that skipped (sibling holds the lease).

    #744 defect 1: a skipped tick must still emit every contract key (zeroed) so a
    coordinator pumping ``t3 loops tick --json`` can read ``["signal_count"]`` /
    ``["errors"]`` unconditionally — the bare ``{"skipped": ...}`` object
    ``KeyError``-ed every structured consumer on each contended beat.
    """
    return {
        "started_at": started_at.isoformat(),
        "signal_count": 0,
        "action_count": 0,
        "statusline_path": "",
        "errors": {},
        "actions": [],
        "skipped": True,
        "skipped_reason": reason,
    }


def _drain_pending_reinstall_guarded() -> None:
    """Re-anchor one deferred self-update reinstall behind a dedicated lease CAS.

    Runs in this fresh per-tick subprocess BEFORE any scanner module imports, so
    the about-to-change modules load fresh with no mixed-code window. The
    ``loop-reinstall`` lease CAS makes at most one concurrent per-loop tick drain
    (a parallel tick that loses the CAS skips), and the short TTL doubles as a
    throttle. A no-op when nothing is pending; :func:`drain_pending_reinstall`
    itself defers while a loop unit is in flight.
    """
    from teatree.loop.self_update_reinstall import drain_pending_reinstall  # noqa: PLC0415

    owner = f"reinstall-{os.getpid()}-{uuid.uuid4().hex}"
    if not LoopLease.objects.acquire(
        _REINSTALL_DRAIN_SLOT, owner=owner, lease_seconds=_REINSTALL_DRAIN_THROTTLE_SECONDS
    ):
        return
    drain_pending_reinstall()


class Command(TyperCommand):
    help = "Run ONE enabled, due DB Loop by name (--loop) — the per-loop primitive each native Claude `/loop` fires."

    def _emit_skip(self, reason: str, *, json_output: bool, statusline_file: Path | None) -> None:
        from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415

        now = dt.datetime.now(tz=dt.UTC)
        _write_tick_meta(now, target=statusline_file)
        if json_output:
            self.stdout.write(json.dumps(_skipped_report_dict(now, reason), indent=2))
        else:
            self.stdout.write(f"SKIP  {reason}")

    def _build_request(self, overlay: str) -> "TickRequest":
        from teatree.loop.tick import TickRequest  # noqa: PLC0415

        if overlay:
            return TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
        return TickRequest(backends=iter_overlay_backends())

    def _emit_report(self, report: "TickReport", *, json_output: bool) -> None:
        if json_output:
            self.stdout.write(json.dumps(_report_to_dict(report), indent=2))
            return
        for name, message in report.errors.items():
            self.stdout.write(f"WARN  {name}: {message}")

    def handle(
        self,
        *,
        statusline_file: Annotated[
            Path | None, typer.Option("--statusline-file", help="Override the statusline output path (test hook).")
        ] = None,
        overlay: Annotated[
            str, typer.Option("--overlay", help="Restrict scanning to the named overlay (default: all).")
        ] = "",
        loop: Annotated[
            str,
            typer.Option(
                "--loop",
                help=(
                    "REQUIRED. Run ONE enabled, due DB Loop by name (#2650) — what each native Claude "
                    "`/loop` fires. Claims the disjoint per-loop `loop:<name>` lease so the per-loop "
                    "loops run in parallel. There is no master tick: omitting --loop is a hard error."
                ),
            ),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the tick report as JSON.")] = False,
    ) -> None:
        if not loop.strip():
            self.stderr.write(
                "t3 loops tick requires --loop <name>. The loop is per-loop only (#2650): one native "
                "Claude `/loop` per enabled DB Loop row, each firing `t3 loops tick --loop <name>` on its "
                "own cadence. There is NO master tick. Run `t3 loops list` to see the loops, then "
                "`t3 loop enable <name>` + register its `/loop` (see `/t3:loops`)."
            )
            raise SystemExit(2)

        # Availability reconciliation (#2544): both drivers of a per-loop tick —
        # the loop-runner daemon's `execute_loop` task and the legacy native
        # Claude `/loop` cron — converge on THIS command (`call_command
        # ("loops_tick", loop=name)` vs `t3 loops tick --loop <name>`), so gating
        # here reconciles both with zero duplicated logic. Only holiday-`away`
        # pauses the self-pump; `autonomous_away` defers questions but keeps the
        # factory self-pumping, so it must NOT park here.
        resolution = availability.resolve_mode()
        if resolution.pauses_self_pump:
            self._emit_skip(
                f"availability={resolution.mode} ({resolution.source}) — self-pump paused, tick parked",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # A per-loop tick (#2650) preflights ONLY its own overlay, gated on the
        # loop being enabled + due — so one overlay's connector outage can't
        # SystemExit an unrelated loop's tick (LOOP-PR-C).
        from teatree.loops.connector_preflight import run_loop_connector_preflight  # noqa: PLC0415

        run_loop_connector_preflight(loop)

        from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

        owner_slot = per_loop_owner_slot(loop)
        tick_mutex = f"{PER_LOOP_TICK_MUTEX_PREFIX}{loop}"

        session_id = current_session_id()
        # The lease ``owner_pid`` MUST be the durable session process, not
        # ``os.getppid()`` of this tick subprocess (the self-pump runs it inside a
        # Bash-tool shell the harness tears down seconds later — anchoring on it
        # collapses the pid-liveness protection back to TTL-only, #1706). The
        # durable session pid comes from the loop registry; ``os.getppid()`` is the
        # fallback only for a direct in-session invocation with no registry record.
        owner_pid = current_session_pid() or os.getppid()
        won, owner_session = LoopLease.objects.claim_ownership(
            owner_slot, session_id=session_id, ttl_seconds=loop_owner_ttl_seconds(), owner_pid=owner_pid
        )
        if not won:
            self._emit_skip(
                f"loop slot {owner_slot!r} not owned by this session — owner is session {owner_session} "
                f"(run `t3 loop claim --slot {owner_slot} --take-over` from the owning session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # Re-anchor a deferred self-update reinstall before any scanner module is
        # imported, so the about-to-change modules load fresh with no mixed-code
        # window. Guarded so parallel per-loop ticks never both reinstall.
        _drain_pending_reinstall_guarded()

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(tick_mutex, owner=owner):
            self._emit_skip(
                "another tick is already running for this loop",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415
        from teatree.loop.tick import run_tick  # noqa: PLC0415
        from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415

        # The statusline dedicated loop line shows every enabled loop with its own
        # next-tick countdown (#1400); install the live DB-backed reader so this
        # per-loop tick's render keeps the full loop line, then reset after the
        # tick so the process-global seam never leaks.
        set_mini_loop_schedules_reader(mini_loop_schedules)
        try:
            request = self._build_request(overlay)
            report = run_tick(request, statusline_path=statusline_file, jobs_builder=_scoped_jobs_builder(loop))
        finally:
            set_mini_loop_schedules_reader(None)
            LoopLease.objects.release(tick_mutex, owner=owner)

        self._emit_report(report, json_output=json_output)
