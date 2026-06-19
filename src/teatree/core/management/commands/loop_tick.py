"""``manage.py loop_tick`` — one tick of the fat loop as a Django management command.

Scans all overlays in parallel, dispatches signals, executes mechanical
actions, and renders the statusline.  Called from ``t3 loop tick`` which
delegates here via subprocess.

#1073 — the FIRST thing ``handle`` does (after the connector preflight)
is the session-scoped loop-owner gate: ``claim_ownership("loop-owner",
session_id=…)``. The per-tick ``loop-tick`` mutex below is keyed on
``pid-<pid>`` and is acquired+released every tick, so between ticks it
rests unowned and ANY session running ``t3 loop tick`` would win its CAS
and do full loop work (the hijack: a foreign session drained the user's
Slack DMs and dispatched reviewers). The persistent ``loop-owner`` claim
is the hard enforcement point — a non-owner SKIPs here, before any
scanner / DM-drain / dispatch, reusing the #744 zeroed-contract SKIP
path. The owning session re-claims every tick; that per-tick re-claim IS
the heartbeat (no renew(), no timer — #54 doctrine), and ``loop-owner``
is NEVER released in the ``finally`` (only ``loop-tick`` is) so its TTL
is its sole release. ``T3_LOOP_OWNER_TTL`` overrides the 1800s default.
"""

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.loop.tick import TickReport, TickRequest

if TYPE_CHECKING:
    from teatree.loops.orchestrator import TickOutcome

# The persistent ``loop-owner`` claim TTL reader lives in
# ``teatree.loop.tick_piggyback`` alongside its sibling per-loop cadence
# readers so the statusline's per-loop next-tick countdown (#1400) can reach
# it without ``teatree.loop`` importing back into ``teatree.core.management``.
# Re-exported here so this command (and its tests) keep the original name.
from teatree.loop.tick_piggyback import _loop_owner_ttl_seconds

type ReportDict = dict[str, Any]


def _registry_jobs_builder(request: "TickRequest", started_at: dt.datetime) -> list[Any]:
    """Drive the live tick's scanner fan-out from the DB ``Loop`` table (#1796 cutover).

    The #2513 cutover: the live fat tick no longer asks ``LoopsConfig``/
    ``MiniLoopMarker`` (code cadence + toml) which scanners run — the ``Loop``
    rows are the single source of truth. A loop runs this tick iff its row is
    ``enabled`` and ``is_due``; :func:`build_loop_table_jobs` resolves each due
    row to its registry ``MiniLoop.build_jobs`` and bumps ``last_run_at``. The
    function name is retained so the statusline-reader wiring and the existing
    ``_registry_jobs_builder`` patch points keep working.
    """
    from teatree.loops.master import build_loop_table_jobs  # noqa: PLC0415

    return build_loop_table_jobs(
        {
            "backends": request.backends,
            "host": request.host,
            "messaging": request.messaging,
            "notion_client": request.notion_client,
            "ready_labels": request.ready_labels,
        },
        now=started_at,
    )


def _report_to_dict(report: TickReport) -> ReportDict:
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

    #744 defect 1: a skipped tick must still emit every contract key
    (zeroed) so a coordinator pumping ``t3 loop tick --json`` can
    ``json.load(...)["signal_count"]`` / ``["errors"]`` unconditionally
    — the bare ``{"skipped": ...}`` object ``KeyError``-ed every
    structured consumer on each contended beat.
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
    help = "Run one tick: scan all overlays, dispatch, render statusline."

    def _emit_skip(self, reason: str, *, json_output: bool, statusline_file: Path | None, suffix: str = "") -> None:
        """Emit the shared #744 zeroed-contract SKIP and refresh tick-meta.

        Both skip paths (the #1073 non-owner gate and the #786 sibling
        ``loop-tick`` mutex) emit the identical shape so structured
        consumers index unconditionally, and both advance ``tick-meta``'s
        ``next_epoch`` so a healthy owner's ticks never decay into a false
        ``tick stale`` on the statusline.
        """
        from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415

        now = dt.datetime.now(tz=dt.UTC)
        _write_tick_meta(now, target=statusline_file)
        if json_output:
            self.stdout.write(json.dumps(_skipped_report_dict(now, reason), indent=2))
        else:
            self.stdout.write(f"SKIP  {reason}{suffix}")

    def handle(
        self,
        *,
        statusline_file: Annotated[
            Path | None,
            typer.Option("--statusline-file", help="Override the statusline output path (test hook)."),
        ] = None,
        overlay: Annotated[
            str,
            typer.Option("--overlay", help="Restrict scanning to the named overlay (default: all)."),
        ] = "",
        slot: Annotated[
            str,
            typer.Option(
                "--slot",
                help=(
                    "Run a SCOPED tick for one dedicated loop (a group of mini-loops): "
                    "claim `loop:<name>` and dispatch only that group's members. Default "
                    "(empty) is the fat tick — claim `loop-owner` and run all mini-loops."
                ),
            ),
        ] = "",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the tick report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.connector_preflight import run_connector_preflight  # noqa: PLC0415

        # Refuse to tick into silent no-ops when a hard-dependency
        # connector is unreachable. Raises SystemExit naming the down
        # connector, before the lease is taken, so the loop fails fast
        # instead of degrading.
        run_connector_preflight(overlay)

        if slot:
            self._handle_scoped(
                slot=slot,
                overlay=overlay,
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return
        self._handle_fat(overlay=overlay, json_output=json_output, statusline_file=statusline_file)

    def _handle_scoped(
        self,
        *,
        slot: str,
        overlay: str,
        json_output: bool,
        statusline_file: Path | None,
    ) -> None:
        """Run one SCOPED tick for the dedicated loop named by ``slot`` (#1838).

        Claims the per-loop owner key ``loop:<name>`` via the SAME
        pid-anchored, hijack-guarded ``claim_ownership`` machinery the global
        ``loop-owner`` uses — so the per-loop gate keeps every #1073 invariant
        (empty-owner guard, pid-liveness, never-released, TTL fallback,
        take-over). The slot is disjoint from ``loop-owner`` and from every
        other ``loop:<group>``, so the global-owner gate and another group's
        scoped tick never block this one. A live foreign owner of this
        group's slot SKIPs. The per-tick mutex is keyed on this group too
        (``loop-tick:<name>``), so two concurrent ticks of the SAME group
        never double-run while DIFFERENT groups run in parallel.
        """
        import os  # noqa: PLC0415

        from teatree.core.loop_lease_manager import per_loop_owner_slot  # noqa: PLC0415
        from teatree.loops.dedicated import dedicated_loop_by_name  # noqa: PLC0415

        dedicated = dedicated_loop_by_name(slot)
        if dedicated is None:
            msg = f"unknown dedicated loop {slot!r} — no such group in teatree.loops.dedicated"
            raise typer.BadParameter(msg)

        owner_slot = per_loop_owner_slot(dedicated.name)
        if not self._claim_owner_gate(owner_slot, json_output=json_output, statusline_file=statusline_file):
            return

        from teatree.core.backend_factory import (  # noqa: PLC0415
            code_host_from_overlay,
            iter_overlay_backends,
            messaging_from_overlay,
        )
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loops.orchestrator import TickRequest  # noqa: PLC0415
        from teatree.loops.scoped_tick import run_scoped_tick  # noqa: PLC0415

        mutex = f"loop-tick:{dedicated.name}"
        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(mutex, owner=owner):
            self._emit_skip(
                "another tick is already running",
                json_output=json_output,
                statusline_file=statusline_file,
                suffix=f" — {mutex} lease held.",
            )
            return
        try:
            if overlay:
                request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
            else:
                request = TickRequest(backends=iter_overlay_backends())
            outcome = run_scoped_tick(dedicated.name, request)
        finally:
            LoopLease.objects.release(mutex, owner=owner)

        self._emit_scoped_outcome(dedicated.name, outcome, json_output=json_output)

    def _claim_owner_gate(self, owner_slot: str, *, json_output: bool, statusline_file: Path | None) -> bool:
        """Claim ``owner_slot`` (global or per-loop) — the #1073 enforcement point.

        Shared by the fat (``loop-owner``) and scoped (``loop:<name>``)
        paths so both gate through the IDENTICAL pid-anchored claim — there
        is no weaker per-loop path. Returns ``True`` when this session owns
        the slot and may proceed; on a non-owner it emits the #744
        zeroed-contract SKIP and returns ``False``.
        """
        import os as _os  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415

        session_id = current_session_id()
        owner_ttl = _loop_owner_ttl_seconds()
        owner_pid = current_session_pid() or _os.getppid()
        won, owner_session = LoopLease.objects.claim_ownership(
            owner_slot, session_id=session_id, ttl_seconds=owner_ttl, owner_pid=owner_pid
        )
        if not won:
            self._emit_skip(
                f"loop slot {owner_slot!r} not owned by this session — owner is session {owner_session} "
                "(run `t3 loop claim --take-over` from the main session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
        return won

    def _emit_scoped_outcome(self, name: str, outcome: "TickOutcome", *, json_output: bool) -> None:
        """Render a scoped tick's outcome."""
        if json_output:
            self.stdout.write(
                json.dumps(
                    {
                        "slot": name,
                        "started_at": outcome.started_at.isoformat(),
                        "dispatched_loops": outcome.dispatched_loops,
                        "skipped_loops": outcome.skipped_loops,
                        "errors": outcome.errors,
                        "action_count": outcome.actions_count,
                    },
                    indent=2,
                )
            )
            return
        for loop_name, message in outcome.errors.items():
            self.stdout.write(f"WARN  {loop_name}: {message}")

    def _handle_fat(self, *, overlay: str, json_output: bool, statusline_file: Path | None) -> None:
        import os  # noqa: PLC0415

        # #1073 — session-scoped loop-owner gate (the enforcement point).
        # The per-tick `loop-tick` CAS below rests `owner=""` between
        # ticks, so a pid-keyed identity lets ANY session that runs
        # `t3 loop tick` (a statusline, an unrelated blog-post session) do
        # full loop work — drain the user's Slack DMs, dispatch reviewers.
        # The persistent `loop-owner` claim closes that: the owning
        # session refreshes it every tick (that per-tick re-claim IS the
        # heartbeat — no renew(), no background timer, #54 doctrine), and
        # a non-owner SKIPs HERE, before any scanner/DM-drain/dispatch. An
        # anonymous caller (session_id=="") with a live owner also SKIPs;
        # with no live owner it RUNS the tick (won=True) but WITHOUT writing
        # the owner row — so a pure-cron / no-session deployment still ticks
        # while the phantom "owned by nobody but not expired" row that let a
        # fresh session hijack the loop can never form (#1073). Liveness is
        # pid-anchored: an alive owner_pid is protected past the TTL, so an
        # owner that is busy past one tick interval is not hijacked. `loop-
        # owner` is NEVER released — its TTL fallback + per-tick re-claim is
        # its sole lifecycle (unlike `loop-tick`, released in the finally
        # below).
        import os as _os  # noqa: PLC0415

        from teatree.core.backend_factory import (  # noqa: PLC0415
            code_host_from_overlay,
            iter_overlay_backends,
            messaging_from_overlay,
        )
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id, current_session_pid  # noqa: PLC0415
        from teatree.loop.tick import run_tick  # noqa: PLC0415

        session_id = current_session_id()
        owner_ttl = _loop_owner_ttl_seconds()
        # The lease's ``owner_pid`` MUST be the long-lived SESSION process,
        # not ``os.getppid()`` here: the Stop self-pump runs this tick inside
        # a Bash-tool shell the harness tears down seconds after the call, so
        # ``os.getppid()`` is a transient pid that is dead almost immediately
        # — anchoring on it collapses the pid-liveness protection back to
        # TTL-only and lets a fresh SessionStart steal a busy owner's loop
        # once the TTL lapses (#1706 root cause). The durable session pid
        # comes from the same loop-registry record the SessionStart hook
        # writes (``_tick_owner_record``); ``os.getppid()`` is the fallback
        # only for a direct in-session invocation with no registry record.
        owner_pid = current_session_pid() or _os.getppid()
        won_owner, owner_session = LoopLease.objects.claim_ownership(
            "loop-owner", session_id=session_id, ttl_seconds=owner_ttl, owner_pid=owner_pid
        )
        if not won_owner:
            self._emit_skip(
                f"loop not owned by this session — owner is session {owner_session} "
                "(run `t3 loop claim --take-over` from the main session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

        # Re-anchor a deferred reinstall before any scanner module is imported,
        # so the about-to-change modules load fresh with no mixed-code window.
        from teatree.loop.self_update_reinstall import drain_pending_reinstall  # noqa: PLC0415

        drain_pending_reinstall()

        # #786 WS2: DB lease/heartbeat replaces the flock/pidfile singleton.
        # The lease row is queryable and reapable by expiry, so loop
        # ownership survives context compaction (re-acquirable) instead of
        # silently vanishing with the flock-holding process. Acquisition is
        # a backend-agnostic atomic CAS (correct on the production SQLite
        # backend — the #786 B1 lesson), so exactly one of N concurrent
        # ticks proceeds; the rest SKIP, same contract as the old flock.
        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-tick", owner=owner):
            self._emit_skip(
                "another tick is already running",
                json_output=json_output,
                statusline_file=statusline_file,
                suffix=" — loop-tick lease held.",
            )
            return
        from teatree.loop.statusline import set_mini_loop_schedules_reader  # noqa: PLC0415
        from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415

        # Bridge the up-stack mini-loop next-fire reader into the statusline
        # for the duration of this tick's render so the dedicated loop line
        # shows every enabled cron with its own countdown (#1400). This
        # command is the only place allowed to import :mod:`teatree.loops`
        # into the statusline (tach graph); the reader is reset afterwards so
        # the process-global seam never leaks past the tick.
        set_mini_loop_schedules_reader(mini_loop_schedules)
        try:
            if overlay:
                request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
            else:
                request = TickRequest(backends=iter_overlay_backends())
            report = run_tick(request, statusline_path=statusline_file, jobs_builder=_registry_jobs_builder)
        finally:
            set_mini_loop_schedules_reader(None)
            LoopLease.objects.release("loop-tick", owner=owner)

        # #1107 Prong B — defense-in-depth safety net. We are PAST the
        # #1073 owner gate (a non-owner SKIPped and returned at line 173,
        # never reaching here) and PAST the `loop-tick` lease finally, so
        # this only fires on the won-owner success path (including the
        # unclaimed auto-claim-for-free case). Each reactive cycle runs
        # behind its OWN dedicated `LoopLease` CAS, so a real dedicated
        # `loop-slack-answer` / `loop-self-improve` slot is never
        # double-run. Pure-cron / no-session deployments — where Prong A
        # still cannot resolve an owner — keep answering user DMs because
        # the won tick drives the reactive cycle directly.
        from teatree.loop.tick_piggyback import run_piggyback_cycles  # noqa: PLC0415

        run_piggyback_cycles()

        result = _report_to_dict(report)
        if json_output:
            self.stdout.write(json.dumps(result, indent=2))
        elif report.errors:
            for name, message in report.errors.items():
                self.stdout.write(f"WARN  {name}: {message}")
