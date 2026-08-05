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
too, so the hook, the ``/t3:health`` skill, and the CLI can never disagree: reactive
slots come from ``teatree.loop.loop_cadences.reactive_slot_directives`` (the
``/loop`` directive).

**Standing directives** (#4166) — the SECOND emission this module owns, and it
has TWO delivery shapes because the seam declares a per-slot ``wakes_session``:

A ``wakes_session`` FALSE slot is written into the turn that is already
happening, as plain context. It costs no turn, so it arms nothing, and it is
gated on the WIDE ``_teatree_engaged`` seam: every engaged session, from its
first prompt, re-delivered no more often than the slot's own interval.

A ``wakes_session`` TRUE slot is rendered as a recurring ``/loop <duration>``
registration, which makes the session wake up on its own. That IS arming the
loop machinery, so it is gated on ``_loop_auto_load_active`` — the marker split
hooks/CLAUDE.md pins (``.t3-engaged`` engages the suggester only; the loop
machinery consults ``.teatree-active``), so a session that merely loaded a
lifecycle skill can never arm a self-waking slot (#256). A ``scope`` of
``attended-singleton`` additionally requires the tick-owner election, so a
host-global slot fires once per host rather than once per session. The SDK lane
is excluded from both shapes — a factory worker is FSM-governed and has no
user-request channel.

Each shape keeps its OWN per-session marker holding per-slot data
(``directives-injected`` carries the last delivery instant per slot;
``directives-registered`` carries the slots already registered), because the
singleton slot's eligibility is decided later in the same handler than the
others' and a shared emit-once marker would strand it.

Neither marker is reaped from here. N engaged sessions is the normal operating
mode, and a session cannot tell a dead peer's marker from a running one's, so a
sweep on every prompt deletes the state its peers throttle on — re-registering
their self-waking slots once per prompt, the runaway the turn budget bounds.
Both suffixes are left to the router's own throttled age sweep, which reaps on
mtime and so reaps only what has stopped being written. ``directives-registered``
is therefore rewritten unchanged on each prompt of its own session, since it is
otherwise written once and would age out underneath the session that holds it.

This half is the Claude-plugin ADAPTER of a harness-neutral model: policy, text,
cadence, scope and delivery cost all belong to
:mod:`teatree.loop.standing_directives`, and this module only maps them onto this
harness's own mechanisms. Another harness delivers the same behaviours by reading
``t3 loop directives --json`` and writing its own adapter, changing no teatree
code — which is why no directive text and no cadence value may appear here.

Crash-proof / fail-open / silent: any failure to bootstrap Django or query the seam
yields ZERO directives, so the handler stays silent — never an exception into the
30s ``UserPromptSubmit`` hook. Reactive-slot resolution is a pure ``os.environ``
read, so the three infra loops still register even when the DB is unreachable.
"""

import json
import re
import sys
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # the contract is OWNED by layer 1; typed here, never redefined
    from teatree.loop.standing_directives import StandingDirectivePayload

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
_SECONDS_PER_MINUTE = 60

_RUN_CMD_RE = re.compile(r"t3 loops tick --loop (?P<name>[^\s`]+)")
_BARE_PROMPT_RE = re.compile(r"^Run `t3 loops tick --loop \S+` in Bash, then briefly report the tick summary\.$")


def _reactive_slot_directives() -> list[str]:
    """The reactive infra ``/loop <duration>`` registrations; fail-open to ``[]`` on ANY error."""
    try:
        from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 deferred cold-hook import

        if not bootstrap_teatree_django():
            return []
        from teatree.loop.loop_cadences import reactive_slot_directives  # noqa: PLC0415 — deferred: cold-hook import

        return list(reactive_slot_directives())
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-open.
        return []


def _worker_owns_cadence() -> bool:
    """Whether ``loop_runner_enabled`` resolves ON (the worker drives the loops); fail-safe OFF."""
    try:
        from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 deferred cold-hook import

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
    directive only when the worker owns the cadence (``loop_runner_enabled`` ON);
    :func:`emit_loop_registrations` prepends it to the reactive-slot emission, so it is
    emitted once per session under the SAME ``loop-pending`` marker that gates that
    emission.
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


def _standing_directives() -> "list[StandingDirectivePayload]":
    """The resolved standing directives as plain payloads; fail-open to ``[]`` on ANY error."""
    from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 deferred cold-hook import

    if not bootstrap_teatree_django():
        return []
    from teatree.loop.standing_directives import resolve_standing_directives  # noqa: PLC0415 — deferred cold import

    return [directive.as_dict() for directive in resolve_standing_directives()]


def _duration_token(seconds: int) -> str:
    """The ``/loop`` duration argument — ``<N>m`` when minute-aligned, else ``<N>s``."""
    if seconds % _SECONDS_PER_MINUTE == 0:
        return f"{seconds // _SECONDS_PER_MINUTE}m"
    return f"{seconds}s"


_INJECTED_MARKER = "directives-injected"
_REGISTERED_MARKER = "directives-registered"


def _marker_data(session_id: str, suffix: str) -> dict[str, float]:
    """The per-slot delivery data in *suffix*, or ``{}`` when absent/unreadable."""
    from hooks.scripts.hook_router import _state_file  # noqa: PLC0415 deferred back-import

    try:
        parsed = json.loads(_state_file(session_id, suffix).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_marker_data(session_id: str, suffix: str, data: dict[str, float]) -> None:
    """Persist *data*; an unwritable state dir is silent, never a raise."""
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    try:
        _ensure_state_dir()
        _state_file(session_id, suffix).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        return


def _inject_context_directives(
    session_id: str, directives: "list[StandingDirectivePayload]", stream: _Writable
) -> bool:
    """Write the zero-cost directives into the turn that is already happening.

    Gated on the WIDE engagement seam: this arms nothing, so narrowing it would
    cost reach and buy no safety. Throttled per slot to the seam's own interval,
    and deliberately NOT emit-once — repeated delivery is the entire mechanism.
    """
    from hooks.scripts.hook_router import _teatree_engaged  # noqa: PLC0415 deferred back-import

    candidates = [d for d in directives if not d["wakes_session"]]
    if not candidates or not _teatree_engaged(session_id):
        return False
    data = _marker_data(session_id, _INJECTED_MARKER)
    now = time.time()
    due = [d for d in candidates if now - data.get(d["slot_id"], 0.0) >= d["cadence_seconds"]]
    if not due:
        return False
    stream.write(f"Standing rules for this session ({len(due)}) — they apply to your next reply:\n")
    for directive in due:
        stream.write(f"  - [{directive['slot_id']}] {directive['text']}\n")
        data[directive["slot_id"]] = now
    _write_marker_data(session_id, _INJECTED_MARKER, data)
    return True


def _register_waking_directives(
    session_id: str, directives: "list[StandingDirectivePayload]", stream: _Writable
) -> bool:
    """Register the self-driving directives as recurring ``/loop``s, once per session.

    Gated on ``_loop_auto_load_active`` because a self-waking slot arms the loop
    machinery, and a host-global slot additionally on the tick-owner election so
    N sessions do not each run it. The slot id is rendered inline so the owner
    can tell which registration a later "stop the loop" would drop.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _claim_loop_ownership,
        _loop_auto_load_active,
        _session_owns_loop,
    )
    from teatree.loop.standing_directives import SCOPE_ATTENDED_SINGLETON  # noqa: PLC0415 deferred cold-hook import

    candidates = [d for d in directives if d["wakes_session"]]
    if not candidates or not _loop_auto_load_active(session_id):
        return False
    data = _marker_data(session_id, _REGISTERED_MARKER)
    pending = [d for d in candidates if d["slot_id"] not in data]
    if any(d["scope"] == SCOPE_ATTENDED_SINGLETON for d in pending):
        _claim_loop_ownership(session_id)
        if not _session_owns_loop(session_id):
            pending = [d for d in pending if d["scope"] != SCOPE_ATTENDED_SINGLETON]
    if not pending:
        if data:
            # Rewritten unchanged so the mtime tracks the SESSION rather than the
            # registration instant: the router's age sweep is what reaps this
            # marker, and reaping a running session's re-registers its slots.
            _write_marker_data(session_id, _REGISTERED_MARKER, data)
        return False
    stream.write(
        f"Session setup: register the {len(pending)} recurring standing slots so they keep firing. "
        f"Run each slash command in this session:\n"
    )
    now = time.time()
    for directive in pending:
        cadence = _duration_token(directive["cadence_seconds"])
        stream.write(f"  - /loop {cadence} [{directive['slot_id']}] {directive['text']}\n")
        data[directive["slot_id"]] = now
    _write_marker_data(session_id, _REGISTERED_MARKER, data)
    return True


def emit_standing_directives_once(session_id: str, stream: _Writable) -> bool:
    """Deliver both standing-directive shapes to *session_id*; returns whether anything was written.

    Fail-open silent throughout: an unreadable marker, an unresolvable seam, a
    raising engagement or ownership probe all emit nothing rather than raising
    into the hot ``UserPromptSubmit`` budget.
    """
    try:
        from hooks.scripts.headless_authoring_gate import LANE_SDK, session_lane  # noqa: PLC0415 deferred back-import

        if not session_id or session_lane() == LANE_SDK:
            return False
        directives = _standing_directives()
        if not directives:
            return False
        injected = _inject_context_directives(session_id, directives, stream)
        registered = _register_waking_directives(session_id, directives, stream)
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-open.
        return False
    return injected or registered


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
