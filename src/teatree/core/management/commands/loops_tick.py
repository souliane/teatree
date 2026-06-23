"""``manage.py loops_tick`` — one master tick driven by the DB ``Loop`` table (#1796).

The cutover surface for ``t3 loops tick``. Claims the singleton ``t3-master``
lease (the renamed ``loop-owner`` — master election: the owning session re-claims
every tick, a non-owner SKIPs), then runs the shared
:func:`teatree.loop.tick.run_tick` pipeline with the DB-``Loop``-driven
``jobs_builder`` so only enabled, due loops fan out. Reap + scan + act + render +
the reactive piggyback cycles are reused unchanged.

``--loop <name>`` (#2650) is the per-loop primitive each native Claude ``/loop``
fires: it scopes the jobs builder to that ONE enabled, due row, claims the
disjoint per-loop ``loop:<name>`` lease (so the N per-loop loops run in parallel
instead of serialising on ``t3-master``), and SKIPs the master piggyback cycles —
those belong to the full fan-out, not a single-loop tick.
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from django_typer.management import TyperCommand

from teatree.core.backend_factory import code_host_from_overlay, iter_overlay_backends, messaging_from_overlay
from teatree.core.connector_preflight import run_connector_preflight
from teatree.core.models import LoopLease
from teatree.loop.tick_piggyback import _loop_owner_ttl_seconds

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.tick import TickRequest
    from teatree.loops.base import BuildJobsContext

_MASTER_SLOT = "t3-master"
_MASTER_TICK_MUTEX = "t3-master-tick"


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


class Command(TyperCommand):
    help = "Run one master tick: run every enabled, due loop (DB-configured); render statusline."

    def _emit_skip(self, reason: str, *, json_output: bool, statusline_file: Path | None) -> None:
        from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415

        now = dt.datetime.now(tz=dt.UTC)
        _write_tick_meta(now, target=statusline_file)
        if json_output:
            self.stdout.write(
                json.dumps({"started_at": now.isoformat(), "skipped": True, "skipped_reason": reason}, indent=2)
            )
        else:
            self.stdout.write(f"SKIP  {reason}")

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
                    "`t3-master`) so the per-loop loops run in parallel, and skips the master "
                    "piggyback cycles. Default (empty) is the full master fan-out."
                ),
            ),
        ] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit the tick report as JSON.")] = False,
    ) -> None:
        run_connector_preflight(overlay)

        from teatree.core.loop_lease_manager import per_loop_owner_slot  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

        owner_slot = per_loop_owner_slot(loop) if loop else _MASTER_SLOT
        tick_mutex = f"loop-tick:{loop}" if loop else _MASTER_TICK_MUTEX

        session_id = current_session_id()
        owner_pid = current_session_pid() or os.getppid()
        won, owner_session = LoopLease.objects.claim_ownership(
            owner_slot, session_id=session_id, ttl_seconds=_loop_owner_ttl_seconds(), owner_pid=owner_pid
        )
        if not won:
            self._emit_skip(
                f"loop slot {owner_slot!r} not owned by this session — owner is session {owner_session} "
                "(run `t3 loop claim --take-over` from the main session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(tick_mutex, owner=owner):
            self._emit_skip("another tick is already running", json_output=json_output, statusline_file=statusline_file)
            return

        from teatree.loop.tick import TickRequest, run_tick  # noqa: PLC0415

        try:
            if overlay:
                request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
            else:
                request = TickRequest(backends=iter_overlay_backends())
            report = run_tick(request, statusline_path=statusline_file, jobs_builder=_scoped_jobs_builder(loop))
        finally:
            LoopLease.objects.release(tick_mutex, owner=owner)

        # The won-tick reactive piggyback cycles (slack-answer / self-improve)
        # belong to the master fan-out, not a single-loop tick — never amplify
        # them once per enabled loop (#2650).
        if not loop:
            from teatree.loop.tick_piggyback import run_piggyback_cycles  # noqa: PLC0415

            run_piggyback_cycles()

        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "started_at": report.started_at.isoformat(),
                        "signal_count": report.signal_count,
                        "action_count": report.action_count,
                        "statusline_path": str(report.statusline_path) if report.statusline_path else "",
                        "errors": report.errors,
                        "actions": [asdict(action) for action in report.actions],
                        "skipped": False,
                    },
                    indent=2,
                )
            )
        elif report.errors:
            for name, message in report.errors.items():
                self.stdout.write(f"WARN  {name}: {message}")
