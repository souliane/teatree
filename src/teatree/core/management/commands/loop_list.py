"""``manage.py loop_list`` — print LIVE loop status from the DB (#1744).

Backs the read-only ``t3 loop list``. Unlike ``t3 loop status`` (which prints
the statusline file written at the *last* tick, so its countdowns are stale),
this rebuilds the state on every call from the cadence ledger, the mini-loop
registry + ``[loops]`` config, and the infra-slot leases — including the
PID-anchored loop-owner liveness. ORM access lives here (a management command,
not a plain typer command) per the project's "anything touching the ORM is a
management command" rule.

Strictly read-only: it issues ORM reads only via
:func:`teatree.loops.live.build_report` and never ticks, claims, acquires,
marks fired, or mutates a row.
"""

import datetime as dt
import json
from typing import Annotated, Any

import typer
from django_typer.management import TyperCommand

from teatree.loops.live import LoopOwnerStatus, LoopStatusEntry, LoopStatusReport, build_report

_NEVER = "—"
_REMEDIATION = "register the `t3 loop tick` cron, or run `t3 loop claim` in a Claude Code session to take ownership"
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return _NEVER
    total = int(seconds)
    if total < _SECONDS_PER_MINUTE:
        return f"{total}s"
    if total < _SECONDS_PER_HOUR:
        return f"{total // _SECONDS_PER_MINUTE}m{total % _SECONDS_PER_MINUTE:02d}s"
    hours, remainder = divmod(total, _SECONDS_PER_HOUR)
    return f"{hours}h{remainder // _SECONDS_PER_MINUTE:02d}m"


def _next_tick_label(entry: LoopStatusEntry, now: dt.datetime) -> str:
    if entry.next_fire_at is None:
        return _NEVER
    if entry.overdue(now):
        return "overdue"
    return f"in {_human_age(entry.due_seconds(now))}"


def _entry_line(entry: LoopStatusEntry, now: dt.datetime) -> str:
    enabled = "enabled" if entry.enabled else "disabled"
    cadence = _human_age(entry.cadence_seconds)
    age = _human_age(entry.age_seconds(now))
    next_tick = _next_tick_label(entry, now)
    line = f"  {entry.name:<22} {enabled:<8} cadence {cadence:<7} last {age:<10} next {next_tick}"
    if entry.kind.value == "infra-slot":
        line += "  held" if entry.held else "  idle"
    return line


def _owner_line(owner: LoopOwnerStatus) -> str:
    if not owner.is_claimed:
        return f"{owner.slot}: unclaimed (no live owner)"
    pid = owner.owner_pid if owner.owner_pid is not None else _NEVER
    liveness = "alive" if owner.pid_is_alive else "dead/unknown"
    state = "live" if owner.is_live else "stale"
    return f"{owner.slot}: session {owner.session_id} (pid {pid} {liveness}) — {state}"


def _per_loop_owner_lines(report: LoopStatusReport) -> list[str]:
    """The cross-session per-loop owner block for ``--all`` (#1834).

    Empty under the single-owner default (no ``loop:<name>`` lease claimed),
    so omitting the header keeps the default view byte-identical.
    """
    if not report.per_loop_owners:
        return []
    return ["per-loop owners:", *(f"  {_owner_line(owner)}" for owner in report.per_loop_owners)]


def _stall_lines(report: LoopStatusReport) -> list[str]:
    if not report.stalled:
        return []
    age = _human_age(report.last_tick_age_seconds)
    return [f"STALLED — last tick {age} ago", f"  hint: {_REMEDIATION}"]


def _render_text(report: LoopStatusReport, *, show_all: bool) -> list[str]:
    now = report.generated_at
    lines = ["infra slots:"]
    lines.extend(_entry_line(entry, now) for entry in report.infra_slots)
    lines.append("mini-loops:")
    lines.extend(_entry_line(entry, now) for entry in report.mini_loops)
    lines.append(_owner_line(report.owner))
    if show_all:
        lines.extend(_per_loop_owner_lines(report))
    lines.extend(_stall_lines(report))
    return lines


def _entry_payload(entry: LoopStatusEntry, now: dt.datetime) -> dict[str, Any]:
    return {
        "name": entry.name,
        "kind": entry.kind.value,
        "enabled": entry.enabled,
        "cadence_seconds": entry.cadence_seconds,
        "last_fired_at": entry.last_fired_at.isoformat() if entry.last_fired_at else "",
        "age_seconds": entry.age_seconds(now),
        "next_fire_at": entry.next_fire_at.isoformat() if entry.next_fire_at else "",
        "never_fired": entry.never_fired,
        "overdue": entry.overdue(now),
        "held": entry.held,
    }


def _owner_payload(owner: LoopOwnerStatus) -> dict[str, Any]:
    return {
        "slot": owner.slot,
        "session_id": owner.session_id,
        "owner_pid": owner.owner_pid,
        "pid_is_alive": owner.pid_is_alive,
        "is_live": owner.is_live,
    }


def _render_json(report: LoopStatusReport, *, show_all: bool) -> str:
    now = report.generated_at
    payload = {
        "generated_at": report.generated_at.isoformat(),
        "tick_cadence_seconds": report.tick_cadence_seconds,
        "last_tick_at": report.last_tick_at.isoformat() if report.last_tick_at else "",
        "last_tick_age_seconds": report.last_tick_age_seconds,
        "stalled": report.stalled,
        "infra_slots": [_entry_payload(entry, now) for entry in report.infra_slots],
        "mini_loops": [_entry_payload(entry, now) for entry in report.mini_loops],
        # The default ``owner`` block keeps its exact #1744 shape (no ``slot``
        # key) so the default ``--json`` output is byte-identical to today.
        # The cross-session per-loop layer (which carries ``slot``) is added
        # only under ``--all``.
        "owner": {
            "session_id": report.owner.session_id,
            "owner_pid": report.owner.owner_pid,
            "pid_is_alive": report.owner.pid_is_alive,
            "is_live": report.owner.is_live,
        },
    }
    if show_all:
        payload["per_loop_owners"] = [_owner_payload(owner) for owner in report.per_loop_owners]
    return json.dumps(payload, indent=2)


class Command(TyperCommand):
    help = "Print LIVE loop status computed from the DB (read-only; #1744)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
        show_all: Annotated[
            bool,
            typer.Option(
                "--all",
                help="Also show the per-loop owning sessions (cross-session health view, #1834).",
            ),
        ] = False,
    ) -> None:
        report = build_report()
        if json_output:
            self.stdout.write(_render_json(report, show_all=show_all))
            return
        for line in _render_text(report, show_all=show_all):
            self.stdout.write(line)
