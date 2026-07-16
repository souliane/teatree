"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Backs ``t3 loop {pause,resume,disable,enable,status} <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

The ``enable``/``disable``/``resume`` verbs move TWO planes in lock-step: the
durable ``LoopState`` control tier (#1913) AND the row-level ``Loop.enabled``
column that the #2584 loop tick reads as its source of truth (``not row.enabled``
skips a loop). The paired write is owned by ONE atomic manager method
(``Loop.objects.disable`` / ``enable`` / ``resume``, holistic 3c#4) — the command
only calls it, so no caller can leave one plane stale (the "reports enabled but
never ticks" bug). ``pause`` is the reversible control-plane hold only — it does
NOT flip the durable ``Loop.enabled`` row; ``resume`` (and ``enable``) then lift
EITHER a pause or a disable and set ``Loop.enabled=True`` on both planes.

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
import json
import logging
import re
from collections.abc import Callable
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

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


def _require_known_loop(name: str, *, json_output: bool, stdout_write: Callable[[str], object]) -> None:
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
    if json_output:
        stdout_write(json.dumps({"name": name, "error": msg}, indent=2))
    else:
        stdout_write(f"ERROR  {msg}")
    raise SystemExit(2)


def _report(name: str, *, json_output: bool, stdout_write: Callable[[str], object]) -> None:
    """Re-read and report the LANDED status after a mutating transition."""
    status = LoopState.objects.status_of(name)
    if json_output:
        stdout_write(json.dumps({"name": name, "status": status.value}, indent=2))
    else:
        stdout_write(f"OK    loop {name!r} is now {status.value}.")


def _report_status(name: str, *, json_output: bool, stdout_write: Callable[[str], object]) -> None:
    """Read-only status report for ``status`` — phrased as a READ, never a mutation.

    The mutation verbs print ``is now <status>``; the read prints
    ``status: <STATUS>`` so an operator inspecting a loop cannot mistake the
    output for a pause/enable that just changed it. The ``--json`` shape is
    identical to :func:`_report` (name + status) so machine consumers are
    unaffected.
    """
    status = LoopState.objects.status_of(name)
    if json_output:
        stdout_write(json.dumps({"name": name, "status": status.value}, indent=2))
    else:
        stdout_write(f"loop {name!r} status: {status.value.upper()}")


def _report_forced(name: str, *, json_output: bool, stdout_write: Callable[[str], object]) -> None:
    """Re-read and report the LANDED forced plane after an override."""
    forced = LoopState.objects.forced_of(name)
    word = "neutral" if forced is None else ("on" if forced else "off")
    if json_output:
        stdout_write(json.dumps({"name": name, "forced": word}, indent=2))
    else:
        stdout_write(f"OK    loop {name!r} override is now {word}.")


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
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        LoopState.objects.pause(name)
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="resume")
    def resume(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED, clearing a pause OR a disable — both planes."""
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        Loop.objects.resume(name)
        _reconcile_timers()
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="disable")
    def disable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Move *name* into the durable DISABLED kill-switch — both planes."""
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        Loop.objects.disable(name)
        _reconcile_timers()
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="enable")
    def enable(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Return *name* to ENABLED (alias of resume) — both planes."""
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        Loop.objects.enable(name)
        _reconcile_timers()
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="override")
    def override(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        state: Annotated[str, typer.Argument(help="on | off | clear.")],
        *,
        for_ttl: Annotated[str, typer.Option("--for", help="TTL for the override (2h/30m/1d).")] = "",
        reason: Annotated[str, typer.Option("--reason", help="Why the override is in force.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Set the emergency FORCED plane for *name* — on/off beats a preset, clear returns to neutral."""
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        normalized = state.strip().lower()
        if normalized not in _OVERRIDE_STATES:
            msg = f"invalid override state {state!r}; use on, off, or clear"
            self.stdout.write(json.dumps({"name": name, "error": msg}) if json_output else f"ERROR  {msg}")
            raise SystemExit(2)
        if normalized == "clear":
            LoopState.objects.clear_override(name)
        else:
            try:
                until = _parse_for(for_ttl)
            except ValueError as exc:
                self.stdout.write(json.dumps({"name": name, "error": str(exc)}) if json_output else f"ERROR  {exc}")
                raise SystemExit(2) from exc
            LoopState.objects.override(name, on=normalized == "on", until=until, reason=reason)
        _report_forced(name, json_output=json_output, stdout_write=self.stdout.write)

    @command(name="status")
    def status(
        self,
        name: Annotated[str, typer.Argument(help="Mini-loop name.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Read *name*'s durable state (ENABLED when no row exists) WITHOUT mutating it."""
        _require_known_loop(name, json_output=json_output, stdout_write=self.stdout.write)
        _report_status(name, json_output=json_output, stdout_write=self.stdout.write)
