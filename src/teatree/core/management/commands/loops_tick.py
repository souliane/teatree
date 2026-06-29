"""``manage.py loops_tick`` — one master tick driven by the DB ``Loop`` table (#1796).

The single tick surface for ``t3 loops tick``. Bare (master) it claims the
singleton ``loop-owner`` lease (master election: the owning session re-claims
every tick, a non-owner SKIPs), drains any deferred self-update reinstall, then
runs the shared :func:`teatree.loop.tick.run_tick` pipeline with the
DB-``Loop``-driven ``jobs_builder`` so only enabled, due loops fan out. Reap +
scan + act + render + the reactive piggyback cycles are reused unchanged. The
legacy ``t3 loop tick`` CLI now delegates here too (#2777 cutover), so this
command carries the full fat-tick behaviour the retired ``loop_tick`` command
held: the deferred-reinstall drain, the statusline mini-loop schedules reader,
and the won-tick piggyback cycles.

``--loop <name>`` (#2650) is the per-loop primitive each native Claude ``/loop``
fires: it scopes the jobs builder to that ONE enabled, due row, claims the
disjoint per-loop ``loop:<name>`` lease (so the N per-loop loops run in parallel
instead of serialising on ``loop-owner``), and SKIPs the master-only steps (the
reinstall drain, the schedules reader, the piggyback cycles) — those belong to
the full fan-out, not a single-loop tick.
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.core.backend_factory import code_host_from_overlay, iter_overlay_backends, messaging_from_overlay
from teatree.core.connector_preflight import run_connector_preflight
from teatree.core.loop_lease_manager import PER_LOOP_TICK_MUTEX_PREFIX, per_loop_owner_slot
from teatree.core.models import LoopLease
from teatree.loop.tick_piggyback import _loop_owner_ttl_seconds

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.tick import TickReport, TickRequest
    from teatree.loops.base import BuildJobsContext

# The single machine-wide master slots: the persistent owner lease the master
# session re-claims every tick (its TTL is its sole release — never released in a
# finally), and the per-tick mutex acquired+released each beat. Per-loop ticks use
# the disjoint ``loop:<name>`` / ``loop-tick:<name>`` namespaces.
_MASTER_SLOT = "loop-owner"
_MASTER_TICK_MUTEX = "loop-tick"

type ReportDict = dict[str, Any]


def _scanner_context(request: "TickRequest") -> "BuildJobsContext":
    return {
        "backends": request.backends,
        "host": request.host,
        "messaging": request.messaging,
        "notion_client": request.notion_client,
        "ready_labels": request.ready_labels,
    }


def _loop_table_jobs_builder(request: "TickRequest", started_at: dt.datetime) -> "list[_ScannerJob]":
    from teatree.loops.master import build_loop_table_jobs  # noqa: PLC0415

    return build_loop_table_jobs(_scanner_context(request), now=started_at)


def _scoped_jobs_builder(only: str) -> "Callable[[TickRequest, dt.datetime], list[_ScannerJob]]":
    """A jobs builder scoped to ONE enabled loop — what ``t3 loops tick --loop`` fires (#2650).

    The empty default returns the module-level full-fan-out builder unchanged; a
    name returns a closure that scopes :func:`build_loop_table_jobs` to that one
    row, so the per-loop ``/loop`` runs exactly its own loop.
    """
    if not only:
        return _loop_table_jobs_builder

    def builder(request: "TickRequest", started_at: dt.datetime) -> "list[_ScannerJob]":
        from teatree.loops.master import build_loop_table_jobs  # noqa: PLC0415

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


class Command(TyperCommand):
    help = "Run one master tick: run every enabled, due loop (DB-configured); render statusline."

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
                    "Run ONE enabled, due DB Loop by name (#2650) — what each native Claude `/loop` "
                    "fires. Claims the disjoint per-loop `loop:<name>` lease (not the singleton "
                    "`loop-owner`) so the per-loop loops run in parallel, and skips the master "
                    "piggyback cycles. Default (empty) is the full master fan-out."
                ),
            ),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the tick report as JSON.")] = False,
    ) -> None:
        run_connector_preflight(overlay)

        from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

        is_master = not loop
        owner_slot = _MASTER_SLOT if is_master else per_loop_owner_slot(loop)
        tick_mutex = _MASTER_TICK_MUTEX if is_master else f"{PER_LOOP_TICK_MUTEX_PREFIX}{loop}"

        session_id = current_session_id()
        # The lease ``owner_pid`` MUST be the durable session process, not
        # ``os.getppid()`` of this tick subprocess (the self-pump runs it inside a
        # Bash-tool shell the harness tears down seconds later — anchoring on it
        # collapses the pid-liveness protection back to TTL-only, #1706). The
        # durable session pid comes from the loop registry; ``os.getppid()`` is the
        # fallback only for a direct in-session invocation with no registry record.
        owner_pid = current_session_pid() or os.getppid()
        won, owner_session = LoopLease.objects.claim_ownership(
            owner_slot, session_id=session_id, ttl_seconds=_loop_owner_ttl_seconds(), owner_pid=owner_pid
        )
        if not won:
            self._emit_skip(
                f"loop slot {owner_slot!r} not owned by this session — owner is session {owner_session} "
                f"(run `t3 loop claim --slot {owner_slot} --take-over` from the main session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # The master re-anchors a deferred self-update reinstall before any scanner
        # module is imported, so the about-to-change modules load fresh with no
        # mixed-code window. A single-loop tick never drains — the master owns it.
        if is_master:
            from teatree.loop.self_update_reinstall import drain_pending_reinstall  # noqa: PLC0415

            drain_pending_reinstall()

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(tick_mutex, owner=owner):
            self._emit_skip("another tick is already running", json_output=json_output, statusline_file=statusline_file)
            return

        from teatree.loop.tick import run_tick  # noqa: PLC0415

        # The master render bridges the mini-loop next-fire reader into the
        # statusline so the dedicated loop line shows every enabled cron with its
        # own countdown (#1400); reset after the tick so the process-global seam
        # never leaks. A single-loop tick does not own the full loop line.
        if is_master:
            from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415
            from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415

            set_mini_loop_schedules_reader(mini_loop_schedules)
        try:
            request = self._build_request(overlay)
            report = run_tick(request, statusline_path=statusline_file, jobs_builder=_scoped_jobs_builder(loop))
        finally:
            if is_master:
                from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415

                set_mini_loop_schedules_reader(None)
            LoopLease.objects.release(tick_mutex, owner=owner)

        # The won-tick reactive piggyback cycles (slack-answer / self-improve)
        # belong to the master fan-out, not a single-loop tick — never amplify them
        # once per enabled loop (#2650).
        if is_master:
            from teatree.loop.tick_piggyback import run_piggyback_cycles  # noqa: PLC0415

            run_piggyback_cycles()

        self._emit_report(report, json_output=json_output)
