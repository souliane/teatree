"""Standing directives re-delivered to every attended session (#4166 Phase 1).

The golden rule — PLAN → IMPLEMENT → COLD REVIEW — is written in three places
and was skipped on a dozen dispatches anyway. The failure is context decay: the
rule holds while it is repeated and lapses when it is not. So the repetition is
automated. A :class:`StandingDirective` is one standing instruction plus the
cadence at which an attended session should be reminded of it.

**This module is harness-neutral, and that is the acceptance test.** It owns the
directive texts, the cadences, the scoping RULE and which slots drive work; it knows
nothing about slash commands, hooks, session markers, or any harness's session model. The
delivery adapter is per-harness; another harness gets the same behaviour by
reading ``t3 loop directives show --json`` and writing only its own adapter —
zero teatree changes.

**Every directive rides a turn that already exists.** None of them wakes a
session or asks it to arm anything recurring, so delivering one costs no turn.
The slots that send the session to work are the ones the dispatch brake below
drops while the active mode masks the dispatch loop off; the golden rule is never
suppressed, because a safety rule that costs nothing has no reason to be rationed.

**The hooks read a publication, never the store.** :func:`publish` writes the
resolution to :mod:`teatree.standing_directives_cache`, called by
``t3 loop directives disable|enable`` and every minute by the worker's
``teatree.loops.standing_directives_publish`` chain, so an owner's edit and a mode
change reach the Django-free hooks without a prompt ever waiting on the DB.

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

from teatree import standing_directives_cache
from teatree.standing_directives_cache import StandingDirectivePayload

logger = logging.getLogger(__name__)

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
    observed to hold while repeated at roughly that rate. The cadence is a MINIMUM
    interval between refreshes rather than a wake-up, so the floor can stay tight.
    """
    return _cadence("T3_GOLDEN_RULE_CADENCE", 300, 60)


def todo_consolidate_cadence_seconds() -> int:
    """``standing-todo-consolidate`` cadence (``T3_TODO_CONSOLIDATE_CADENCE``, default 1800s, floor 600).

    Half-hourly rather than tight: a consolidation pass can trigger real work, so
    firing it often would preempt the work already in flight. The floor is what
    actually bounds how often a session is sent back to it.
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
    "Consolidate the todo list — it must DRAIN across a session, not only grow. CAPTURE: "
    "confirm every user request from this session is captured as a task, none dropped. DRAIN: "
    "for every OPEN task: a durable record already satisfies it — a filed issue, a merged PR, "
    "a posted handoff, a request already executed — CLOSE it, naming the record. An open task "
    "with the work already done elsewhere is a FALSE OPEN; re-working or re-asking it wastes "
    "effort. Reconcile from durable state FIRST (the task list, `t3 <overlay> questions list`, "
    "filed issues, the forge, the session snapshots); rescan the transcript ONLY if something "
    "cannot be accounted for from durable state. Then implement the outstanding user requests, "
    "oldest first. Net growth across the session means DRAIN was skipped."
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
    """One standing instruction, its cadence, who receives it, and whether it sends the session to work.

    ``drives_work`` is what the dispatch brake reads: a slot that sends the session
    to work has nothing to do while the active mode masks the dispatch loop off.
    """

    slot_id: str
    cadence_seconds: Callable[[], int]
    default_text: str
    scope: str
    drives_work: bool


#: The standing directives, in delivery order. Slot 1 deliberately covers BOTH
#: coupled failures the owner named — the orchestrator implementing without a
#: plan, AND the orchestrator doing the work itself instead of routing it. It is
#: also the only slot that drives no work, which is why the brake never drops it.
STANDING_DIRECTIVES: tuple[StandingDirective, ...] = (
    StandingDirective(
        "standing-golden-rule",
        golden_rule_cadence_seconds,
        _GOLDEN_RULE_TEXT,
        scope=SCOPE_ATTENDED,
        drives_work=False,
    ),
    StandingDirective(
        "standing-todo-consolidate",
        todo_consolidate_cadence_seconds,
        _TODO_CONSOLIDATE_TEXT,
        scope=SCOPE_ATTENDED,
        drives_work=True,
    ),
    # The PR board is one board per host: N sessions each driving it means N cold
    # reviews per PR per pass and two sub-agents on one branch, not faster merges.
    StandingDirective(
        "standing-pr-board",
        pr_board_cadence_seconds,
        _PR_BOARD_TEXT,
        scope=SCOPE_ATTENDED_SINGLETON,
        drives_work=True,
    ),
)


@dataclass(frozen=True, slots=True)
class ResolvedDirective:
    """A directive with its cadence and text resolved — the cross-harness contract."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str

    def as_dict(self) -> StandingDirectivePayload:
        """The four-key payload ``t3 loop directives show --json`` prints and the hooks read."""
        return {
            "slot_id": self.slot_id,
            "cadence_seconds": self.cadence_seconds,
            "text": self.text,
            "scope": self.scope,
        }


def compiled_directives() -> list[ResolvedDirective]:
    """Every slot with its compiled default text — what a hook delivers while nothing is published.

    Reads no store, so it resolves in a process that has no Django at all.
    """
    return [
        ResolvedDirective(directive.slot_id, directive.cadence_seconds(), directive.default_text, directive.scope)
        for directive in STANDING_DIRECTIVES
    ]


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

#: A mode that masks this loop OFF dispatches no work, so a slot that sends the
#: session to work has nothing to send it to.
DISPATCH_LOOP = "dispatch"


def _drop_record(_record: logging.LogRecord) -> bool:
    return False


@contextmanager
def _mode_read_unlogged() -> Iterator[None]:
    """Silence the mode read's own fail-open WARNINGs for the duration of one call.

    The publish chain resolves every minute, so a degraded store would log a full
    traceback once a minute, for a probe whose failure is already handled here.

    Named-logger filters rather than ``logging.disable``, which is process-global:
    :func:`resolve_standing_directives` runs in the ``t3 worker``'s pool, where a
    blanket disable would swallow a concurrent thread's own unrelated logging for
    the length of this read.
    """
    resolvers = [logging.getLogger(name) for name in _MODE_READ_LOGGERS]
    for resolver in resolvers:
        resolver.addFilter(_drop_record)
    try:
        yield
    finally:
        for resolver in resolvers:
            resolver.removeFilter(_drop_record)


def _dispatch_masked() -> bool:
    """Whether the active mode masks the dispatch loop OFF — then nothing should be driving work.

    Reads the MERGED mode (#4196), never the override/schedule layer: that layer stops
    at ``None`` when neither governs, so it cannot see the configured default mode.

    Fails OPEN to delivering: an unresolvable mode still delivers.
    """
    from teatree.core.mode_resolution import resolve_active_mode  # noqa: PLC0415 — deferred: Django-free defaults

    try:
        with _mode_read_unlogged():
            return resolve_active_mode().state_for(DISPATCH_LOOP) is False
    except Exception:
        logger.debug("the active mode is unreadable — the brake stays off", exc_info=True)
        return False


def resolve_standing_directives() -> list[ResolvedDirective]:
    """Every switched-on standing directive with its cadence and text resolved.

    Fails open to the compiled defaults: a directive that cannot be looked up is
    still worth delivering, and a store outage must not silently drop the golden
    rule from every session. A mode that masks dispatch off drops the slots that
    drive work and only those — the golden rule keeps reaching a session that is
    deliberately idle, because it costs that session nothing. The brake is read
    only once a work-driving slot has survived text resolution: with those slots
    switched off there is nothing for it to drop.
    """
    try:
        overrides = _override_texts()
    except Exception:
        logger.debug("the directive overrides are unreadable — the compiled defaults resolve", exc_info=True)
        overrides = {}
    resolved = [
        (directive, text)
        for directive in STANDING_DIRECTIVES
        if (text := _resolve_text(directive, overrides)) is not None
    ]
    if any(directive.drives_work for directive, _ in resolved) and _dispatch_masked():
        resolved = [(directive, text) for directive, text in resolved if not directive.drives_work]
    return [
        ResolvedDirective(directive.slot_id, directive.cadence_seconds(), text, directive.scope)
        for directive, text in resolved
    ]


def publish() -> bool:
    """Publish the resolved directives for the hooks; ``False`` when that resolution is already published.

    Resolved inside the cache's publisher lock, so the worker's chain and an owner's ``disable`` never
    interleave a stale read with a newer write.
    """
    return standing_directives_cache.publish(
        lambda: [directive.as_dict() for directive in resolve_standing_directives()]
    )


__all__ = [
    "MAX_DIRECTIVE_CHARS",
    "SCOPE_ATTENDED",
    "SCOPE_ATTENDED_SINGLETON",
    "STANDING_DIRECTIVES",
    "ResolvedDirective",
    "StandingDirective",
    "compiled_directives",
    "golden_rule_cadence_seconds",
    "override_prompt_name",
    "pr_board_cadence_seconds",
    "publish",
    "resolve_standing_directives",
    "todo_consolidate_cadence_seconds",
]
