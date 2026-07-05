"""Register the owner session's reactive infra ``/loop``s at session start.

Bare sibling of ``hook_router`` (hooks/CLAUDE.md: NEW hook logic lives in a
sibling module, never in the shrink-only-capped router). The owner session's
``UserPromptSubmit`` handler delegates here.

**Reactive infra loops** — the three always-on reactive slots (Slack-answer,
self-improve, drain-queue). They have NO DB ``Loop`` row and a sub-minute cadence
a cron cannot express, so each registers via the ``/loop <duration>`` form. There
is no master tick to piggyback them onto, so the owner registers the three here —
otherwise they would be dead until a manual ``t3 loop <slot> start``.

PR-28 retired the native ``/loop`` cron mirror of the DB ``Loop`` rows: the
singleton ``t3 worker`` now owns the per-loop tick cadence by default, so the
owner session no longer emits a ``CronCreate`` per enabled DB loop. Only the
reactive infra slots (front-end seam, not CronCreate) are registered here. The
pure prompt recognisers (:func:`is_bare_loop_tick_prompt` / :func:`loop_name_from_prompt`)
STAY — ``hook_router`` and ``cron_tracking`` still classify a per-loop tick prompt
(fired by the worker's subprocess tick, or by any stale pre-flip cron not yet
deleted) without importing teatree.

The directive source of truth is the seam the ``t3 loop <slot> start`` CLI reads
too, so the hook, the ``/t3:loops`` skill, and the CLI can never disagree: reactive
slots come from ``teatree.loop.loop_cadences.reactive_slot_directives`` (the
``/loop`` directive).

Crash-proof / fail-open / silent: any failure to bootstrap Django or query the seam
yields ZERO directives, so the handler stays silent — never an exception into the
30s ``UserPromptSubmit`` hook. Reactive-slot resolution is a pure ``os.environ``
read, so the three infra loops still register even when the DB is unreachable.
"""

import re
import sys
from typing import Protocol

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# imports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("loop_registrations", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.loop_registrations", sys.modules[__name__])


class _Writable(Protocol):
    def write(self, text: str, /) -> object: ...


# The per-loop run command + its full bare-prompt shape, kept in sync with the
# worker's subprocess-tick argv (``python -m teatree loops_tick --loop <name>``) and
# the manual ``t3 loops tick --loop <name>``. Used to RECOGNISE a fired per-loop tick
# prompt from the hot ``UserPromptSubmit`` path WITHOUT importing teatree (no Django).
_RUN_CMD_RE = re.compile(r"t3 loops tick --loop (?P<name>[^\s`]+)")
_BARE_PROMPT_RE = re.compile(r"^Run `t3 loops tick --loop \S+` in Bash, then briefly report the tick summary\.$")


def _reactive_slot_directives() -> list[str]:
    """The reactive infra ``/loop <duration>`` registrations; fail-open to ``[]`` on ANY error."""
    try:
        from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415

        if not bootstrap_teatree_django():
            return []
        from teatree.loop.loop_cadences import reactive_slot_directives  # noqa: PLC0415

        return list(reactive_slot_directives())
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-open.
        return []


def _worker_owns_cadence() -> bool:
    """Whether ``loop_runner_enabled`` resolves ON (the worker drives the loops); fail-safe OFF."""
    try:
        from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 (deferred: cold-hook import)

        if not bootstrap_teatree_django():
            return False
        from teatree.config import get_effective_settings  # noqa: PLC0415 (deferred: cold-hook import)

        return bool(get_effective_settings().loop_runner_enabled)
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-safe OFF.
        return False


def cron_decommission_directive() -> str | None:
    """The one-time CronDelete reminder for stale pre-flip native ``/loop`` crons, or ``None``.

    PR-28 retired the native ``/loop`` cron mirror and made the ``t3 worker`` the default
    cadence owner. A box that ran BEFORE the flip may still have per-loop
    ``t3 loops tick --loop <name>`` crons registered in the harness. The worker's per-loop
    ``loop-tick:<name>`` mutex + the ``Loop.last_run_at`` CAS make a stale cron BENIGN (a
    wasted subprocess, never double work), but it should be cleaned up. Returns the
    directive only when the worker owns the cadence (``loop_runner_enabled`` ON); the
    owner-session handler emits it once per session (a ``cron-decommission`` marker).
    """
    if not _worker_owns_cadence():
        return None
    return (
        "Session setup: the `t3 worker` now owns the loop-tick cadence (PR-28 retired the "
        "native `/loop` cron mirror). A pre-flip box may still have stale per-loop crons "
        "registered. Run CronList once, then CronDelete every job whose prompt contains the "
        "backtick-terminated token `` `t3 loops tick --loop <name>` `` — the worker drives "
        "those loops now, so the crons only ever waste a subprocess (the per-loop mutex + CAS "
        "make them benign). If CronList shows none, nothing to do."
    )


def emit_loop_registrations(stream: _Writable) -> bool:
    """Emit the owner session's reactive infra ``/loop`` registrations + the cron-decommission reminder.

    Returns whether anything was emitted so the caller writes the once-per-session
    ``loop-pending`` marker (the emit-once key for both). PR-28 retired the
    per-enabled-DB-loop ``CronCreate`` emission (the worker owns that cadence now); it
    instead emits the one-time CronDelete reminder for stale pre-flip crons when the
    worker owns the cadence. Nothing to register AND no reminder emits NOTHING and
    returns ``False`` so the owner session stays silent.
    """
    emitted = False
    directive = cron_decommission_directive()
    if directive:
        stream.write(directive + "\n")
        emitted = True
    reactive = _reactive_slot_directives()
    if reactive:
        _write_reactive_prose(reactive, stream)
        emitted = True
    return emitted


def _write_reactive_prose(directives: list[str], stream: _Writable) -> None:
    stream.write(
        f"Session setup: register the {len(directives)} reactive infra loops — sub-minute cadence, so use "
        f"the `/loop <duration>` form (NOT a cron). Run each slash command in this session:\n"
    )
    for directive in directives:
        stream.write(f"  - {directive}\n")


def is_bare_loop_tick_prompt(stripped: str) -> bool:
    """True when *stripped* is a PURE per-loop tick prompt (#2650) — no residual user content."""
    return bool(_BARE_PROMPT_RE.match(stripped))


def loop_name_from_prompt(prompt: str) -> str | None:
    """The ``--loop <name>`` a per-loop tick prompt runs, or ``None`` when it is not one."""
    match = _RUN_CMD_RE.search(prompt)
    return match.group("name") if match else None
