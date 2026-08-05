"""Standing directives re-delivered to every attended session (#4166 Phase 1).

The golden rule — PLAN → IMPLEMENT → COLD REVIEW — is written in three places
and was skipped on a dozen dispatches anyway. The failure is context decay: the
rule holds while it is repeated and lapses when it is not. So the repetition is
automated. A :class:`StandingDirective` is one standing instruction plus the
cadence at which an attended session should be reminded of it.

**This module is harness-neutral, and that is the acceptance test.** It owns the
directive texts, the cadences, the scoping RULE and whether a slot needs the
session to wake up; it knows nothing about slash commands, hooks, session
markers, or any harness's session model. The delivery adapter is per-harness;
another harness gets all three behaviours by reading ``t3 loop directives
--json`` and writing only its own adapter — zero teatree changes.

**Cost follows the delivery shape, which is why ``wakes_session`` is per slot.**
A directive that only needs to be in context when the agent next acts costs
nothing to deliver on a turn that already exists; one that must drive work when
nobody prompted costs a whole turn. Only the second kind is bounded by the
floors, by the singleton scope, and by the self-pump brake below — the zero-turn
kind is never suppressed, because a safety rule that costs nothing has no reason
to be rationed.

Text resolution is data, not code: a compiled default per slot, overridable by a
:class:`~teatree.core.models.Prompt` row named ``standing-directive:<slot_id>``
so the owner can edit and version a directive without a deploy. An override that
strips to empty switches that slot OFF (``t3 loop directives disable <slot>``
writes exactly that, through ``revise`` so the disable is versioned and
reversible); one longer than :data:`MAX_DIRECTIVE_CHARS` is ignored in favour of
the compiled default, so the per-session context cost stays bounded whatever the
store holds. Every failure — an unreachable store, an unbootstrapped Django —
degrades to the compiled defaults, so the directives resolve on a box with no
database at all.
"""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypedDict

from teatree.core.mode_resolution import resolve_active_mode


class StandingDirectivePayload(TypedDict):
    """The cross-harness directive contract — what ``t3 loop directives --json`` prints."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str
    wakes_session: bool


#: Every attended session — a session a human is present for. Each harness
#: decides what that means for its own runtime.
SCOPE_ATTENDED = "attended"

#: Exactly ONE attended session per host. For work that is global rather than
#: per-session: N sessions each driving it independently is N duplicate agent
#: runs contending over the same branches, not N times the throughput.
SCOPE_ATTENDED_SINGLETON = "attended-singleton"

#: Hard per-directive length cap. The aggregate context cost of the standing
#: directives is (texts x injections per hour), so the text side is capped here
#: and the injection side is capped by the cadences below.
MAX_DIRECTIVE_CHARS = 1200

_SECONDS_PER_HOUR = 3600

_OVERRIDE_PROMPT_PREFIX = "standing-directive:"


def override_prompt_name(slot_id: str) -> str:
    """The ``Prompt`` row name whose body overrides *slot_id*'s compiled default."""
    return f"{_OVERRIDE_PROMPT_PREFIX}{slot_id}"


def _cadence(env_var: str, default: int, floor: int) -> int:
    raw = os.environ.get(env_var, str(default)).strip()
    if not raw:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        return default


def golden_rule_cadence_seconds() -> int:
    """``standing-golden-rule`` cadence (``T3_GOLDEN_RULE_CADENCE``, default 300s, floor 60).

    Five minutes is the owner's own measured re-statement interval — the rule was
    observed to hold while repeated at roughly that rate. This slot costs no turn,
    so the cadence is a MINIMUM interval between refreshes rather than a wake-up,
    and the floor can stay tight.
    """
    return _cadence("T3_GOLDEN_RULE_CADENCE", 300, 60)


def todo_consolidate_cadence_seconds() -> int:
    """``standing-todo-consolidate`` cadence (``T3_TODO_CONSOLIDATE_CADENCE``, default 1800s, floor 600).

    Half-hourly rather than tight: a consolidation pass can trigger real work, so
    firing it often would preempt the work already in flight. The floor is what
    actually bounds the cost — a session configured at the old 300s floor could
    wake itself twelve times an hour for this slot alone.
    """
    return _cadence("T3_TODO_CONSOLIDATE_CADENCE", 1800, 600)


def pr_board_cadence_seconds() -> int:
    """``standing-pr-board`` cadence (``T3_PR_BOARD_CADENCE``, default 600s, floor 300).

    Ten minutes is CI-paced — faster than a pipeline completes, so no PR waits a
    whole cycle for its next advance, and tighter than a pipeline cannot observe a
    new result at all. The floor is set where the cadence stops buying information.
    """
    return _cadence("T3_PR_BOARD_CADENCE", 600, 300)


_GOLDEN_RULE_TEXT = (
    "Golden rule: PLAN → IMPLEMENT (via sub-agents) → COLD REVIEW. "
    "Never dispatch an implementing agent (t3:coder / t3:debugger / t3:tester / t3:e2e) on "
    "unplanned work — dispatch t3:planner to record a PlanArtifact first, or record "
    '`t3 <overlay> ticket skip-planning <id> --reason "<why>"` for genuinely trivial work. '
    "A ticket description, acceptance criteria, or review findings are NOT a plan. "
    "And the orchestrator ROUTES — it never implements itself: delegate every implementation, "
    "investigation and publishing action to a sub-agent, and never edit, test, or publish from "
    "the orchestrating turn."
)

_TODO_CONSOLIDATE_TEXT = (
    "Consolidate the todo list: confirm every user request from this session is captured as a "
    "task and none has been dropped. Reconcile from durable state FIRST (the task list, "
    "`t3 <overlay> questions list`, filed issues, the session snapshots); rescan the transcript "
    "ONLY if a request cannot be accounted for from durable state. Then implement all "
    "outstanding user requests, oldest first."
)

_PR_BOARD_TEXT = (
    "Drive the PR board — merge properly, but promptly: every open PR must advance every pass. "
    "Clean and no verdict → dispatch an independent cold review at the live head. "
    "Fresh merge_safe at the LIVE head with green CI → merge now via the keystone "
    "(`review record` → `ticket clear` → `ticket merge <clear_id>`; the standing substrate "
    "authorization applies, so do not ask per-PR). Red CI → dispatch a fix. "
    "Conflicted → update the branch, then RE-review (a verdict does not survive a rebase). "
    "Promptness accelerates DISPATCHING, never verdicts: never a raw forge-CLI merge, "
    "never merge over a hold, maker ≠ checker."
)


@dataclass(frozen=True, slots=True)
class StandingDirective:
    """One standing instruction, its cadence, who receives it, and what it costs.

    ``wakes_session`` is the cost declaration a delivery adapter reads: ``False``
    means the directive only has to be present when the session next acts, so it
    rides a turn that already exists; ``True`` means it must drive work with
    nobody prompting, so delivering it costs a turn.
    """

    slot_id: str
    cadence_seconds: Callable[[], int]
    default_text: str
    scope: str
    wakes_session: bool


#: The standing directives, in delivery order. Slot 1 deliberately covers BOTH
#: coupled failures the owner named — the orchestrator implementing without a
#: plan, AND the orchestrator doing the work itself instead of routing it. It is
#: also the only zero-cost slot, which is why it is the one that reaches widest.
STANDING_DIRECTIVES: tuple[StandingDirective, ...] = (
    StandingDirective(
        "standing-golden-rule",
        golden_rule_cadence_seconds,
        _GOLDEN_RULE_TEXT,
        scope=SCOPE_ATTENDED,
        wakes_session=False,
    ),
    StandingDirective(
        "standing-todo-consolidate",
        todo_consolidate_cadence_seconds,
        _TODO_CONSOLIDATE_TEXT,
        scope=SCOPE_ATTENDED,
        wakes_session=True,
    ),
    # The PR board is one board per host: N sessions each driving it means N cold
    # reviews per PR per pass and two sub-agents on one branch, not faster merges.
    StandingDirective(
        "standing-pr-board",
        pr_board_cadence_seconds,
        _PR_BOARD_TEXT,
        scope=SCOPE_ATTENDED_SINGLETON,
        wakes_session=True,
    ),
)


@dataclass(frozen=True, slots=True)
class ResolvedDirective:
    """A directive with its cadence and text resolved — the cross-harness contract."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str
    wakes_session: bool

    def as_dict(self) -> StandingDirectivePayload:
        """The five-key payload ``t3 loop directives --json`` prints."""
        return {
            "slot_id": self.slot_id,
            "cadence_seconds": self.cadence_seconds,
            "text": self.text,
            "scope": self.scope,
            "wakes_session": self.wakes_session,
        }


def _override_texts() -> dict[str, str]:
    """Owner-edited directive bodies by slot id, from the ``Prompt`` store."""
    from teatree.core.models import Prompt  # noqa: PLC0415 — deferred: keeps the module DB-free at import

    names = {override_prompt_name(d.slot_id): d.slot_id for d in STANDING_DIRECTIVES}
    rows = Prompt.objects.filter(name__in=names).values_list("name", "body")
    return {names[name]: body for name, body in rows}


def _resolve_text(directive: StandingDirective, overrides: dict[str, str]) -> str | None:
    """The text to deliver for *directive*, or ``None`` when the slot is switched off."""
    if directive.slot_id not in overrides:
        return directive.default_text
    override = overrides[directive.slot_id].strip()
    if not override:
        return None
    return override if len(override) <= MAX_DIRECTIVE_CHARS else directive.default_text


#: The two resolver layers a mode read spans, each of which logs its own fail-open
#: WARNING with ``exc_info=True``.
_MODE_READ_LOGGERS = ("teatree.core.mode_resolution", "teatree.loop.preset_resolution")


def _drop_record(_record: logging.LogRecord) -> bool:
    return False


@contextmanager
def _mode_read_unlogged() -> Iterator[None]:
    """Silence the mode read's own fail-open WARNINGs for the duration of one call.

    The caller below is documented silent: its only stderr is the delivery hook's,
    so a degraded store would print a full traceback into the owner's terminal on
    every prompt, for a probe whose failure is already handled here.

    Named-logger filters rather than ``logging.disable``, which is process-global:
    :func:`resolve_standing_directives` is a public export reachable from the
    ``t3 worker``'s pool, where a blanket disable would swallow a concurrent
    thread's own unrelated logging for the length of this read.
    """
    loggers = [logging.getLogger(name) for name in _MODE_READ_LOGGERS]
    for logger in loggers:
        logger.addFilter(_drop_record)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(_drop_record)


def _self_pump_paused() -> bool:
    """Whether the active mode pauses the self-pump — a self-waking directive IS one.

    Reads the MERGED mode (#4196), never the L3/L2 preset layer: that layer stops
    at ``None`` when neither an override nor a schedule slot governs, so it cannot
    see the L0 default mode and — the case that matters here — cannot see the
    live-presence upgrade. Braking a self-waking directive on it would suppress
    the rule at a scheduled away slot the owner is demonstrably typing into.

    Fails OPEN to delivering. The polarity matches this module's doctrine
    everywhere else: an unresolvable state degrades to delivering the rule, never
    to silently suppressing it. Only a positively-resolved away mode brakes.
    """
    try:
        with _mode_read_unlogged():
            return resolve_active_mode().pauses_self_pump
    except Exception:  # noqa: BLE001 — an unresolvable mode degrades to delivering.
        return False


def resolve_standing_directives() -> list[ResolvedDirective]:
    """Every switched-on standing directive with its cadence and text resolved.

    Fails open to the compiled defaults: a directive that cannot be looked up is
    still worth delivering, and a store outage must not silently drop the golden
    rule from every session. A mode that pauses the self-pump drops the
    self-waking slots and only those — the zero-turn rule keeps reaching a session
    that is deliberately idle, because it costs that session nothing. The brake is
    read only once a self-waking slot has survived text resolution: with the
    waking slots switched off there is nothing for it to drop, and this runs on
    every prompt of every engaged session.
    """
    try:
        overrides = _override_texts()
    except Exception:  # noqa: BLE001 — an unreachable store degrades to the compiled defaults.
        overrides = {}
    resolved = [
        ResolvedDirective(
            directive.slot_id,
            directive.cadence_seconds(),
            text,
            scope=directive.scope,
            wakes_session=directive.wakes_session,
        )
        for directive in STANDING_DIRECTIVES
        if (text := _resolve_text(directive, overrides)) is not None
    ]
    if not any(directive.wakes_session for directive in resolved):
        return resolved
    return [directive for directive in resolved if not directive.wakes_session] if _self_pump_paused() else resolved


def self_woken_turns_per_hour() -> dict[str, int]:
    """The self-woken turn budget the resolved directives cost, split by scope.

    ``per_session`` multiplies by the number of attended sessions;
    ``per_host_singleton`` does not, because exactly one session delivers it. The
    zero-turn slots contribute nothing by construction, so this is the whole cost
    of the mechanism and the number the budget test pins.
    """
    budget = {"per_session": 0, "per_host_singleton": 0}
    for directive in resolve_standing_directives():
        if not directive.wakes_session:
            continue
        key = "per_host_singleton" if directive.scope == SCOPE_ATTENDED_SINGLETON else "per_session"
        budget[key] += _SECONDS_PER_HOUR // directive.cadence_seconds
    return budget


__all__ = [
    "MAX_DIRECTIVE_CHARS",
    "SCOPE_ATTENDED",
    "SCOPE_ATTENDED_SINGLETON",
    "STANDING_DIRECTIVES",
    "ResolvedDirective",
    "StandingDirective",
    "StandingDirectivePayload",
    "golden_rule_cadence_seconds",
    "override_prompt_name",
    "pr_board_cadence_seconds",
    "resolve_standing_directives",
    "self_woken_turns_per_hour",
    "todo_consolidate_cadence_seconds",
]
