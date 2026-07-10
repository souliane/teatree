"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Backs ``t3 loop {pause,resume,disable,enable,status} <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

The ``enable``/``disable``/``resume`` verbs move TWO planes in lock-step inside
one transaction: the durable ``LoopState`` control tier (#1913) AND the
row-level ``Loop.enabled`` column that the #2584 loop tick reads as its source
of truth (``not row.enabled`` skips a loop). Before this, ``enable`` wrote only
``LoopState`` and left ``Loop.enabled`` stale, so the verb reported "now enabled"
while the loop was never ticked. ``pause`` is the reversible control-plane
hold only — it does NOT flip the durable ``Loop.enabled`` row (a paused loop
returns to running with ``resume`` without re-enabling a row that may have been
deliberately ``disable``d).

Each transition is the atomic, idempotent ``LoopState`` upsert paired with the
idempotent ``Loop.enabled`` update; the command re-reads and reports the LANDED
status so the operator sees the verified state rather than an echo of the request.

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

import json
import logging
from collections.abc import Callable
from typing import Annotated

import typer
from django.db import transaction
from django_typer.management import TyperCommand, command

from teatree.core.models import Loop, LoopState

logger = logging.getLogger(__name__)


def _reconcile_timers() -> None:
    """Reconcile the loop-timer chains after an enable/disable — best-effort.

    The enable/disable chokepoint (#1796): enabling a loop creates its chain head
    at once and disabling prunes its queued timers, so the change takes effect
    without waiting for the next ~5-minute reconciler pass. Never fatal — the timer
    rows only fire when a worker drains them, so a reconcile failure here degrades
    to the periodic reconciler catching up.
    """
    try:
        from teatree.loops.timer_reconciler import ensure_loop_timers  # noqa: PLC0415

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


def _report(name: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
    """Re-read and report the LANDED status after a mutating transition."""
    status = LoopState.objects.status_of(name)
    if json_output:
        stdout_write(json.dumps({"name": name, "status": status.value}, indent=2))
    else:
        stdout_write(f"OK    loop {name!r} is now {status.value}.")


def _report_status(name: str, *, json_output: bool, stdout_write) -> None:  # noqa: ANN001
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
        with transaction.atomic():
            LoopState.objects.resume(name)
            Loop.objects.set_enabled(name, enabled=True)
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
        with transaction.atomic():
            LoopState.objects.disable(name)
            Loop.objects.set_enabled(name, enabled=False)
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
        with transaction.atomic():
            LoopState.objects.enable(name)
            Loop.objects.set_enabled(name, enabled=True)
        _reconcile_timers()
        _report(name, json_output=json_output, stdout_write=self.stdout.write)

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
