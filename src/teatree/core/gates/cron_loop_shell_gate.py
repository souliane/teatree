"""Refuse a harness cron/wakeup that shells a ``t3 loop`` command the worker owns.

The recurrence this closes (#2663): a session-setup hook asks the owner session
to register ``drain-queue`` / ``slack-answer`` / ``self-improve`` as harness
crons, the agent obliges, and every tick is a guaranteed no-op because the live
``t3 worker`` already holds the singleton those cycles stand down against. It
cost ~40 turns the first time and recurred twice more, each time after memory
decay archived the rule — so the remediation is a gate, not more prose.

Two distinct shapes, and only one of them is conditional:

*   **tick** — ``t3 loops tick --loop <name>``. PR-28 retired the native
    ``/loop`` cron mirror and left NO fallback plane, so a cron driving a
    per-loop tick is always wrong; the worker drives every enabled ``Loop`` row.
*   **reactive** — ``t3 loop <slot> run``. These have no DB ``Loop`` row and a
    session genuinely drives them on a box with no worker, so this shape is
    refused only while a worker holds the singleton.

This module is the pure, Django-free decision core; the thin PreToolUse hook
(``hooks/scripts/cron_loop_shell_gate.py``) supplies the worker-liveness probe
and the deny emission.
"""

import re
from dataclasses import dataclass

# Vendored rather than imported: ``teatree.core`` must not depend on
# ``teatree.loop`` (tach backwards edge). The parity test in
# ``tests/teatree_core/gates/test_cron_loop_shell_gate.py`` pins these against
# ``teatree.loop.loop_cadences.REACTIVE_SLOTS`` so a rename cannot drift.
REACTIVE_RUN_SLOTS: tuple[str, ...] = ("slack-answer", "self-improve", "drain-queue")

_TICK_RE = re.compile(r"\bt3 loops? tick\b(?:\s+--loop\s+\S+)?")
_REACTIVE_RE = re.compile(rf"\bt3 loop (?:{'|'.join(REACTIVE_RUN_SLOTS)}) run\b")
_OK_TOKEN_RE = re.compile(r"\[cron-loop-ok:\s*(\S[^\]]*?)\s*\]")

#: Only the head of a field is scanned for the escape token, mirroring the other
#: per-call escapes so a token buried in a long body cannot silently relax the gate.
_TOKEN_SCAN_CHARS = 512

TICK = "tick"
REACTIVE = "reactive"


@dataclass(frozen=True, slots=True)
class CronLoopShellFinding:
    """One cron/wakeup prompt that shells a ``t3 loop`` command the worker owns."""

    command: str
    shape: str
    worker_alive: bool


def find_cron_loop_shell(prompt: str, *, worker_alive: bool) -> CronLoopShellFinding | None:
    """Return a finding iff *prompt* schedules a loop command the worker owns, else None.

    The tick shape fires unconditionally; the reactive shape fires only while a
    worker is alive, so a genuine pre-flip / worker-down box can still register
    the reactive slots the session is the only driver for.
    """
    if not prompt:
        return None
    if tick := _TICK_RE.search(prompt):
        return CronLoopShellFinding(command=tick.group(0), shape=TICK, worker_alive=worker_alive)
    reactive = _REACTIVE_RE.search(prompt)
    if reactive is None or not worker_alive:
        return None
    return CronLoopShellFinding(command=reactive.group(0), shape=REACTIVE, worker_alive=worker_alive)


def deny_reason(finding: CronLoopShellFinding) -> str:
    """The operator-facing refusal: what was scheduled, why it is a no-op, and the normal way."""
    if finding.shape == TICK:
        why = (
            "PR-28 retired the native `/loop` cron mirror — the singleton `t3 worker` owns the "
            "per-loop tick cadence and there is no fallback plane, so this cron only ever wastes "
            "a subprocess."
        )
        remedy = (
            "Enable the loop the normal way (`t3 loop enable <name>`) and ensure a worker runs (`t3 worker ensure`)."
        )
    else:
        why = (
            "a live `t3 worker` holds the worker singleton, so this cycle already runs as a worker "
            "chain and every scheduled tick stands down as a guaranteed no-op."
        )
        remedy = (
            "Check `t3 worker status`; it is already driven. Register this slot only on a box whose "
            "worker is down (`t3 loop enable <name>` for a DB loop)."
        )
    return (
        f"BLOCKED: this cron/wakeup shells `{finding.command}` — {why} {remedy} "
        "Override this one call with a `[cron-loop-ok: <reason>]` token in the prompt, or disable "
        "the gate with `t3 <overlay> gate cron-loop-shell disable`."
    )


def cron_loop_ok_reason(text: str) -> str | None:
    """The reason from a ``[cron-loop-ok: <reason>]`` escape token in *text*, else None."""
    if not text:
        return None
    match = _OK_TOKEN_RE.search(text[:_TOKEN_SCAN_CHARS])
    return match.group(1).strip() or None if match else None


__all__ = [
    "REACTIVE",
    "REACTIVE_RUN_SLOTS",
    "TICK",
    "CronLoopShellFinding",
    "cron_loop_ok_reason",
    "deny_reason",
    "find_cron_loop_shell",
]
