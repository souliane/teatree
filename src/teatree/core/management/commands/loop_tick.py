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
from typing import Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.loop.tick import TickReport

type ReportDict = dict[str, Any]


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


_LOOP_OWNER_TTL_DEFAULT = 1800


def _loop_owner_ttl_seconds() -> int:
    """The persistent ``loop-owner`` claim TTL (``T3_LOOP_OWNER_TTL``, default 1800s).

    Parsed defensively like ``cli.loop._cadence_for_loop_slot``: a blank
    or non-integer override degrades to the default rather than crashing
    the tick; the floor of 60s keeps a fat-fingered tiny TTL from making
    the owner lapse mid-tick.
    """
    import os  # noqa: PLC0415

    raw = os.environ.get("T3_LOOP_OWNER_TTL", str(_LOOP_OWNER_TTL_DEFAULT)).strip()
    if not raw:
        return _LOOP_OWNER_TTL_DEFAULT
    try:
        return max(60, int(raw))
    except ValueError:
        return _LOOP_OWNER_TTL_DEFAULT


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
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the tick report as JSON."),
        ] = False,
    ) -> None:
        import os  # noqa: PLC0415

        from teatree.core.backend_factory import (  # noqa: PLC0415
            code_host_from_overlay,
            iter_overlay_backends,
            messaging_from_overlay,
        )
        from teatree.core.connector_preflight import run_connector_preflight  # noqa: PLC0415
        from teatree.core.models import LoopLease  # noqa: PLC0415
        from teatree.loop.session_identity import current_session_id  # noqa: PLC0415
        from teatree.loop.tick import TickRequest, run_tick  # noqa: PLC0415

        # Refuse to tick into silent no-ops when a hard-dependency
        # connector is unreachable. Raises SystemExit naming the down
        # connector, before the lease is taken, so the loop fails fast
        # instead of degrading.
        run_connector_preflight(overlay)

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
        # first run / no owner auto-claims for free via the CAS (matches
        # the hook-layer "no owner → you are owner"). `loop-owner` is
        # NEVER released — its TTL + per-tick re-claim is its sole
        # lifecycle (unlike `loop-tick`, released in the finally below).
        session_id = current_session_id()
        owner_ttl = _loop_owner_ttl_seconds()
        won_owner, owner_session = LoopLease.objects.claim_ownership(
            "loop-owner", session_id=session_id, ttl_seconds=owner_ttl
        )
        if not won_owner:
            self._emit_skip(
                f"loop not owned by this session — owner is session {owner_session} "
                "(run `t3 loop claim --take-over` from the main session to take over).",
                json_output=json_output,
                statusline_file=statusline_file,
            )
            return

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
        try:
            if overlay:
                request = TickRequest(host=code_host_from_overlay(), messaging=messaging_from_overlay())
            else:
                request = TickRequest(backends=iter_overlay_backends())
            report = run_tick(request, statusline_path=statusline_file)
        finally:
            LoopLease.objects.release("loop-tick", owner=owner)

        result = _report_to_dict(report)
        if json_output:
            self.stdout.write(json.dumps(result, indent=2))
        else:
            self.stdout.write(f"OK    {report.signal_count} signal(s), {report.action_count} action(s).")
            if report.errors:
                for name, message in report.errors.items():
                    self.stdout.write(f"WARN  {name}: {message}")
            if report.statusline_path:
                self.stdout.write(f"      statusline → {report.statusline_path}")
