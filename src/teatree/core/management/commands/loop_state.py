"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Backs ``t3 loop {pause,resume,disable,enable,status} <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

Every verb here writes ONE plane — the durable ``LoopState`` hold (#1913), the
emergency brake above every other layer. ``pause`` and ``disable`` set it;
``resume`` and ``enable`` lift either. The manual override (``Loop.enabled``) is a
DIFFERENT layer with its own command (``t3 loop override``) and its own sole writer
(``Loop.objects.set_manual_override``), so lifting a hold hands the loop back to
whatever the override — or, beneath it, the active preset — already said.

The command re-reads and reports the LANDED status so the operator sees the
verified state rather than an echo of the request.

``status`` is the one strictly READ-ONLY verb: it reports the current durable
state and writes nothing. Its output is phrased as a read (``status: <STATUS>``),
never the mutation verbs' ``is now <status>``, so inspecting a loop can never be
mistaken for a pause/enable that just changed it.

Every verb first validates the NAME against the real ``Loop`` rows (#3117): an
unknown name is refused with a non-zero exit before any ``LoopState`` is read or
written, so a typo can never report success and pause nothing, and
``status <typo>`` can never resolve to the fall-through ``ENABLED`` for a loop
that does not exist.
"""

import datetime as dt
import logging
import re
from typing import IO, Annotated, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.models import Loop, LoopState

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_OVERRIDE_STATES = {"on", "off", "clear"}


def _parse_for(raw: str) -> dt.datetime | None:
    """Resolve a ``--for`` TTL (``2h``/``30m``/``1d``) to an absolute instant, or ``None`` when empty."""
    raw = raw.strip()
    if not raw:
        return None
    match = _DURATION_RE.match(raw)
    if match is None:
        msg = f"invalid --for duration {raw!r}; use forms like 2h, 30m, 1d"
        raise ValueError(msg)
    return timezone.now() + dt.timedelta(seconds=int(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2)])


def _reconcile_timers() -> None:
    """Reconcile the loop-timer chains after an enable/disable — best-effort.

    The enable/disable chokepoint (#1796): enabling a loop creates its chain head
    at once and disabling prunes its queued timers, so the change takes effect
    without waiting for the next ~5-minute reconciler pass. Never fatal — the timer
    rows only fire when a worker drains them, so a reconcile failure here degrades
    to the periodic reconciler catching up.
    """
    try:
        from teatree.loops.timer_reconciler import ensure_loop_timers  # noqa: PLC0415 — deferred: lazy command import

        ensure_loop_timers()
    except Exception:
        logger.debug(
            "ensure_loop_timers after loop-state change failed — periodic reconciler will catch up", exc_info=True
        )


def _out_err(command: TyperCommand) -> tuple[IO[str], IO[str]]:
    return cast("IO[str]", command.stdout), cast("IO[str]", command.stderr)


def _require_known_loop(command: TyperCommand, name: str, *, json_output: bool) -> None:
    """Refuse a NAME with no matching ``Loop`` row before any ``LoopState`` read/write (#3117).

    Every verb — the mutating ``pause``/``resume``/``disable``/``enable`` and the
    read-only ``status`` — validates the name against the real ``Loop`` rows here
    so a typo (``t3 loop pause <typo>``) can never report success and pause
    nothing, and ``loop-state <typo>`` can never resolve to a fall-through
    ``ENABLED`` for a loop that does not exist. Exits ``2`` (the loop-command
    refusal convention), naming the loop and pointing at ``t3 loops list``.
    """
    if Loop.objects.filter(name=name).exists():
        return
    msg = f"no loop named {name!r} — run `t3 loops list` to see the known loops"
    out, err = _out_err(command)
    emit({"name": name, "error": msg}, json_output=json_output, out=out, err=err, human=f"ERROR  {msg}")
    raise SystemExit(2)


def _report(command: TyperCommand, name: str, *, json_output: bool) -> None:
    """Re-read and report the LANDED status after a mutating transition."""
    status = LoopState.objects.status_of(name)
    out, err = _out_err(command)
    emit(
        {"name": name, "status": status.value},
        json_output=json_output,
        out=out,
        err=err,
        human=f"OK    loop {name!r} is now {status.value}.",
    )


def _report_status(command: TyperCommand, name: str, *, json_output: bool) -> None:
    """Read-only status report for ``status`` — phrased as a READ, never a mutation.

    The mutation verbs print ``is now <status>``; the read prints
    ``status: <STATUS>`` so an operator inspecting a loop cannot mistake the
    output for a pause/enable that just changed it. The ``--json`` shape is
    identical to :func:`_report` (name + status) so machine consumers are
    unaffected.
    """
    status = LoopState.objects.status_of(name)
    out, err = _out_err(command)
    emit(
        {"name": name, "status": status.value},
        json_output=json_output,
        out=out,
        err=err,
        human=f"loop {name!r} status: {status.value.upper()}",
    )


def _report_override(command: TyperCommand, name: str, *, json_output: bool) -> None:
    """Re-read and report the LANDED manual override, plus the EFFECTIVE verdict it produced."""
    from teatree.loops.enable_verdict import EnablePlanes  # noqa: PLC0415 — deferred: ORM-backed resolver

    row = Loop.objects.get(name=name)
    word = "none" if row.enabled is None else ("on" if row.enabled else "off")
    admitted = EnablePlanes.resolve().admits(name)
    out, err = _out_err(command)
    emit(
        {"name": name, "override": word, "reason": row.override_reason, "admitted": admitted},
        json_output=json_output,
        out=out,
        err=err,
        human=f"OK    loop {name!r} override is now {word}; it {'RUNS' if admitted else 'does NOT run'}.",
    )


class Command(TyperCommand):
    help = "Pause, resume, disable, enable, or inspect a mini-loop's durable state (#1913)."

    @command(name="pause")
    def pause(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name (e.g. review, ship, dispatch).")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the reversible PAUSED hold."""
        _require_known_loop(self, name, json_output=json_output)
        LoopState.objects.pause(name)
        _report(self, name, json_output=json_output)

    @command(name="resume")
    def resume(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED, clearing a pause OR a disable — both planes."""
        _require_known_loop(self, name, json_output=json_output)
        Loop.objects.release(name)
        _reconcile_timers()
        _report(self, name, json_output=json_output)

    @command(name="disable")
    def disable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the durable DISABLED kill-switch — both planes."""
        _require_known_loop(self, name, json_output=json_output)
        Loop.objects.hold(name)
        _reconcile_timers()
        _report(self, name, json_output=json_output)

    @command(name="enable")
    def enable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED (alias of resume) — both planes."""
        _require_known_loop(self, name, json_output=json_output)
        Loop.objects.release(name)
        _reconcile_timers()
        _report(self, name, json_output=json_output)

    @command(name="override")
    def override(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        state: Annotated[str, typer.Argument(help="on | off | clear.")],
        *,
        lift_by: Annotated[str, typer.Option("--lift-by", help="When you expect to lift it (2h/30m/1d).")] = "",
        reason: Annotated[str, typer.Option("--reason", help="Why the override is in force. Required.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Set the MANUAL override for *name* — on/off beats the preset, clear hands it back."""
        _require_known_loop(self, name, json_output=json_output)
        out, err = _out_err(self)
        normalized = state.strip().lower()
        if normalized not in _OVERRIDE_STATES:
            msg = f"invalid override state {state!r}; use on, off, or clear"
            emit({"name": name, "error": msg}, json_output=json_output, out=out, err=err, human=f"ERROR  {msg}")
            raise SystemExit(2)
        try:
            expected_lift_at = _parse_for(lift_by)
            Loop.objects.set_manual_override(
                name,
                runs=None if normalized == "clear" else normalized == "on",
                reason=reason,
                expected_lift_at=expected_lift_at,
            )
        except ValueError as exc:
            emit({"name": name, "error": str(exc)}, json_output=json_output, out=out, err=err, human=f"ERROR  {exc}")
            raise SystemExit(2) from exc
        # The manual layer outranks the preset in the enable verdict, so a write here
        # changes chain membership at once — same as the hold verbs above. Nothing expires
        # it: `--lift-by` is what the override watcher reminds against (A5/A7).
        _reconcile_timers()
        _report_override(self, name, json_output=json_output)

    @command(name="status")
    def status(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Read *name*'s durable state (ENABLED when no row exists) WITHOUT mutating it."""
        _require_known_loop(self, name, json_output=json_output)
        _report_status(self, name, json_output=json_output)
