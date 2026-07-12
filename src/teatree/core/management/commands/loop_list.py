"""``manage.py loop_list`` — print LIVE loop status from the DB (#1744).

Backs the read-only ``t3 loop list``. Unlike ``t3 loop status`` (which prints
the statusline file written at the *last* tick, so its countdowns are stale),
this rebuilds the state on every call from the cadence ledger, the mini-loop
registry + ``[loops]`` config, and the infra-slot leases — including the
PID-anchored t3-master liveness. ORM access lives here (a management command,
not a plain typer command) per the project's "anything touching the ORM is a
management command" rule.

Strictly read-only: it issues ORM reads only via
:func:`teatree.loops.live.build_report` and never ticks, claims, acquires,
marks fired, or mutates a row.
"""

import datetime as dt
import json
from collections.abc import Sequence
from typing import IO, Annotated, Any, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.session_identity import current_session_id
from teatree.core.table_output import print_table
from teatree.loops.live import LoopOwnerStatus, LoopStatusEntry, LoopStatusReport, build_report, owned_per_loop_owners

_NEVER = "—"
_REMEDIATION = (
    "re-register each enabled loop's `/loop` via the `/t3:loops` skill, or run `t3 loop claim` "
    "in a Claude Code session to take ownership (force a one-off render with `t3 loops tick`)"
)
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


def _held_cell(entry: LoopStatusEntry) -> str:
    if entry.kind.value == "infra-slot":
        return "held" if entry.held else "idle"
    if entry.held:
        # A held mini-loop keeps enabled=True + a live countdown — the marker is its only "won't tick" signal.
        return "held"
    # A #3159 preset can flip a mini-loop with NO LoopState hold: the disagreement
    # between the effective `admitted` verdict and the base `enabled` flag is the
    # preset's doing — masking a base-enabled loop off, or forcing a base-disabled one on.
    if entry.enabled and not entry.admitted:
        return "masked"
    if not entry.enabled and entry.admitted:
        return "forced-on"
    return ""


def _entry_row(entry: LoopStatusEntry, now: dt.datetime) -> list[str]:
    return [
        entry.name,
        "enabled" if entry.enabled else "disabled",
        _human_age(entry.cadence_seconds),
        _human_age(entry.age_seconds(now)),
        _next_tick_label(entry, now),
        _held_cell(entry),
    ]


_ENTRY_HEADERS = ["Loop", "State", "Cadence", "Last", "Next", "Held"]


def _render_entries_table(title: str, entries: Sequence[LoopStatusEntry], now: dt.datetime, stream: IO[str]) -> None:
    print_table(_ENTRY_HEADERS, [_entry_row(entry, now) for entry in entries], title=title, stream=stream)


def _owner_line(owner: LoopOwnerStatus) -> str:
    if not owner.is_claimed:
        return f"{owner.slot}: unclaimed (no live owner)"
    pid = owner.owner_pid if owner.owner_pid is not None else _NEVER
    liveness = "alive" if owner.pid_is_alive else "dead/unknown"
    state = "live" if owner.is_live else "stale"
    return f"{owner.slot}: session {owner.session_id} (pid {pid} {liveness}) — {state}"


def _resolve_per_loop_owners(report: LoopStatusReport, *, show_all: bool) -> tuple[LoopOwnerStatus, ...]:
    """The per-loop owner set to render — full under ``--all``, scoped by default (#1834 WI-2).

    Without ``--all`` the block scopes to the CURRENT session's owned loops
    via :func:`owned_per_loop_owners` (which fails open to the full set when
    no session resolves); ``--all`` keeps the cross-session health view.
    Either way an empty report (the single-owner default, no ``loop:<name>``
    lease) yields ``()`` so the per-loop block is absent and the output is
    byte-identical to today.
    """
    if show_all:
        return report.per_loop_owners
    return owned_per_loop_owners(report, current_session_id())


def _per_loop_owner_lines(owners: tuple[LoopOwnerStatus, ...]) -> list[str]:
    """The per-loop owner block. Empty when there are no per-loop owners.

    Under the single-owner default (no ``loop:<name>`` lease claimed) the
    resolved set is empty, so omitting the header keeps the default view
    byte-identical to today.
    """
    if not owners:
        return []
    return ["per-loop owners:", *(f"  {_owner_line(owner)}" for owner in owners)]


def _stall_lines(report: LoopStatusReport) -> list[str]:
    if not report.stalled:
        return []
    age = _human_age(report.last_tick_age_seconds)
    return [f"STALLED — last tick {age} ago", f"  hint: {_REMEDIATION}"]


def _status_lines(report: LoopStatusReport, *, show_all: bool) -> list[str]:
    """The non-tabular status lines rendered below the loop tables.

    The owner, per-loop-owner and stall blocks are status prose (one live
    session's health), not record rows, so they stay lines rather than joining
    the loop tables.
    """
    lines = [_owner_line(report.owner)]
    lines.extend(_per_loop_owner_lines(_resolve_per_loop_owners(report, show_all=show_all)))
    lines.extend(_stall_lines(report))
    return lines


def _entry_payload(entry: LoopStatusEntry, now: dt.datetime) -> dict[str, Any]:
    return {
        "name": entry.name,
        "kind": entry.kind.value,
        "enabled": entry.enabled,
        "admitted": entry.admitted,
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
        # key) so the single-owner default ``--json`` output is byte-identical
        # to today. The per-loop layer (which carries ``slot``) is added only
        # when there are per-loop owners to show: scoped to the current
        # session by default, the full cross-session set under ``--all``
        # (#1834 WI-2).
        "owner": {
            "session_id": report.owner.session_id,
            "owner_pid": report.owner.owner_pid,
            "pid_is_alive": report.owner.pid_is_alive,
            "is_live": report.owner.is_live,
        },
    }
    per_loop_owners = _resolve_per_loop_owners(report, show_all=show_all)
    if per_loop_owners:
        payload["per_loop_owners"] = [_owner_payload(owner) for owner in per_loop_owners]
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
        stream = cast("IO[str]", self.stdout)
        now = report.generated_at
        _render_entries_table("infra slots", report.infra_slots, now, stream)
        _render_entries_table("mini-loops", report.mini_loops, now, stream)
        for line in _status_lines(report, show_all=show_all):
            self.stdout.write(line)
