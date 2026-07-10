"""``manage.py loops_toggle`` — enable/disable a single DB ``Loop`` row.

Backs ``t3 loops enable <name>`` / ``t3 loops disable <name>``. ORM access lives
here (a management command, not a plain typer command) per the project's
"anything touching the ORM is a management command" rule.

This is the per-instance FLEET seam: each teatree instance has its own DB, so
flipping ``Loop.enabled`` on one instance turns a loop on/off there alone —
without touching operator intent shared across the fleet. It therefore moves
ONLY the row-level ``Loop.enabled`` column, deliberately distinct from the
singular ``t3 loop enable``/``disable`` (which also writes the durable
``LoopState`` control plane). The #2584 unified admission verdict
(``teatree.loops.loop_table._loop_admitted``, shared by the live tick AND the
loop-timer chains) gates a loop on ``row.enabled``, so a disabled row is skipped
at both admission points. The enable/disable then reconciles the timer chains
(:func:`teatree.loops.timer_reconciler.ensure_loop_timers`) so a newly-enabled
loop gets its chain head at once and a disabled one's queued timers are pruned
at once, rather than firing no-op idle polls until the periodic reconciler
catches up.

Unknown loop name is a hard error (exit 2) naming the valid loops — the plural
toggle refuses to seed intent for a name with no ``Loop`` row, unlike the
control-plane verbs.
"""

import json
import logging
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Loop

logger = logging.getLogger(__name__)

_UNKNOWN_LOOP_EXIT = 2


def _reconcile_timers() -> None:
    """Reconcile the loop-timer chains after a toggle — best-effort, never fatal.

    Enabling a loop creates its chain head at once and disabling prunes its
    queued timers, so the change takes effect without waiting for the periodic
    reconciler. A failure here degrades to that reconciler catching up — the
    timer rows only ever fire when a worker drains them, and a disabled row is
    already skipped by the shared admission verdict.
    """
    try:
        from teatree.loops.timer_reconciler import ensure_loop_timers  # noqa: PLC0415 — deferred timer edge

        ensure_loop_timers()
    except Exception:
        logger.debug("ensure_loop_timers after loops toggle failed — periodic reconciler will catch up", exc_info=True)


class Command(TyperCommand):
    help = "Enable or disable a single DB-configured autonomous loop by name (the per-instance fleet seam)."

    @command()
    def enable(
        self,
        name: Annotated[str, typer.Argument(help="Loop name (see `t3 loops list`).")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Set ``Loop.enabled = True`` for *name* on this instance (idempotent)."""
        self._set_enabled(name, enabled=True, json_output=json_output)

    @command()
    def disable(
        self,
        name: Annotated[str, typer.Argument(help="Loop name (see `t3 loops list`).")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Set ``Loop.enabled = False`` for *name* on this instance (idempotent)."""
        self._set_enabled(name, enabled=False, json_output=json_output)

    def _set_enabled(self, name: str, *, enabled: bool, json_output: bool) -> None:
        if not Loop.objects.filter(name=name).exists():
            valid = ", ".join(Loop.objects.values_list("name", flat=True)) or "(none)"
            self.stderr.write(f"Unknown loop {name!r}. Valid loops: {valid}.")
            raise SystemExit(_UNKNOWN_LOOP_EXIT)
        Loop.objects.set_enabled(name, enabled=enabled)
        _reconcile_timers()
        state = "enabled" if enabled else "disabled"
        if json_output:
            self.stdout.write(json.dumps({"name": name, "enabled": enabled}, indent=2))
        else:
            self.stdout.write(f"{name}: {state}")
