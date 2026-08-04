"""Standing directives re-delivered to every attended session (#4166 Phase 1).

The golden rule — PLAN → IMPLEMENT → COLD REVIEW — is written in three places
and was skipped on a dozen dispatches anyway. The failure is context decay: the
rule holds while it is repeated and lapses when it is not. So the repetition is
automated. A :class:`StandingDirective` is one standing instruction plus the
cadence at which an attended session should be reminded of it.

**This module is harness-neutral, and that is the acceptance test.** It owns the
directive texts, the cadences, and the attended scoping RULE; it knows nothing
about slash commands, hooks, session markers, or any harness's session model.
The delivery adapter is per-harness (teatree's Claude plugin renders these as
recurring ``/loop`` registrations); another harness gets all three behaviours by
reading ``t3 loop directives --json`` and writing only its own adapter — zero
teatree changes.

Text resolution is data, not code: a compiled default per slot, overridable by a
:class:`~teatree.core.models.Prompt` row named ``standing-directive:<slot_id>``
so the owner can edit and version a directive without a deploy. An override that
strips to empty switches that slot OFF; one longer than
:data:`MAX_DIRECTIVE_CHARS` is ignored in favour of the compiled default, so the
per-session context cost stays bounded whatever the store holds. Every failure —
an unreachable store, an unbootstrapped Django — degrades to the compiled
defaults, so the directives resolve on a box with no database at all.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict


class StandingDirectivePayload(TypedDict):
    """The cross-harness directive contract — what ``t3 loop directives --json`` prints."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str


#: The scoping RULE, stated in neutral vocabulary: these are for a session a
#: human is attending. Each harness decides what that means for its own runtime.
STANDING_DIRECTIVE_SCOPE = "attended"

#: Hard per-directive length cap. The aggregate context cost of the standing
#: directives is (texts x injections per hour), so the text side is capped here
#: and the injection side is capped by the cadences below.
MAX_DIRECTIVE_CHARS = 1200

_OVERRIDE_PROMPT_PREFIX = "standing-directive:"
_SECONDS_PER_MINUTE = 60


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
    observed to hold while repeated at roughly that rate.
    """
    return _cadence("T3_GOLDEN_RULE_CADENCE", 300, 60)


def todo_consolidate_cadence_seconds() -> int:
    """``standing-todo-consolidate`` cadence (``T3_TODO_CONSOLIDATE_CADENCE``, default 1800s, floor 300).

    Half-hourly rather than tight: a consolidation pass can trigger real work, so
    firing it often would preempt the work already in flight.
    """
    return _cadence("T3_TODO_CONSOLIDATE_CADENCE", 1800, 300)


def pr_board_cadence_seconds() -> int:
    """``standing-pr-board`` cadence (``T3_PR_BOARD_CADENCE``, default 600s, floor 120).

    Ten minutes is CI-paced — faster than a pipeline completes, so no PR waits a
    whole cycle for its next advance.
    """
    return _cadence("T3_PR_BOARD_CADENCE", 600, 120)


_GOLDEN_RULE_TEXT = (
    "Golden rule: PLAN → IMPLEMENT (via sub-agents) → COLD REVIEW. "
    "Never dispatch an implementing agent (t3:coder / t3:debugger / t3:tester / t3:e2e) on "
    "unplanned work — dispatch t3:planner to record a PlanArtifact first, or record "
    '`t3 <overlay> ticket skip-planning <id> --reason "<why>"` for genuinely trivial work. '
    "A ticket description, acceptance criteria, or review findings are NOT a plan. "
    "And the orchestrator ROUTES — it never implements itself: delegate every implementation, "
    "investigation and publishing action to a sub-agent, per the /t3:interactive workflow."
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
    """One standing instruction and the cadence an attended session re-reads it at."""

    slot_id: str
    cadence_seconds: Callable[[], int]
    default_text: str


#: The standing directives, in delivery order. Slot 1 deliberately covers BOTH
#: coupled failures the owner named — the orchestrator implementing without a
#: plan, AND the orchestrator doing the work itself instead of routing it.
STANDING_DIRECTIVES: tuple[StandingDirective, ...] = (
    StandingDirective("standing-golden-rule", golden_rule_cadence_seconds, _GOLDEN_RULE_TEXT),
    StandingDirective("standing-todo-consolidate", todo_consolidate_cadence_seconds, _TODO_CONSOLIDATE_TEXT),
    StandingDirective("standing-pr-board", pr_board_cadence_seconds, _PR_BOARD_TEXT),
)


@dataclass(frozen=True, slots=True)
class ResolvedDirective:
    """A directive with its cadence and text resolved — the cross-harness contract."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str = STANDING_DIRECTIVE_SCOPE

    def as_dict(self) -> StandingDirectivePayload:
        """The four-key payload ``t3 loop directives --json`` prints."""
        return {
            "slot_id": self.slot_id,
            "cadence_seconds": self.cadence_seconds,
            "text": self.text,
            "scope": self.scope,
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


def resolve_standing_directives() -> list[ResolvedDirective]:
    """Every switched-on standing directive with its cadence and text resolved.

    Fails open to the compiled defaults: a directive that cannot be looked up is
    still worth delivering, and a store outage must not silently drop the golden
    rule from every session.
    """
    try:
        overrides = _override_texts()
    except Exception:  # noqa: BLE001 — an unreachable store degrades to the compiled defaults.
        overrides = {}
    resolved: list[ResolvedDirective] = []
    for directive in STANDING_DIRECTIVES:
        text = _resolve_text(directive, overrides)
        if text is None:
            continue
        resolved.append(ResolvedDirective(directive.slot_id, directive.cadence_seconds(), text))
    return resolved


__all__ = [
    "MAX_DIRECTIVE_CHARS",
    "STANDING_DIRECTIVES",
    "STANDING_DIRECTIVE_SCOPE",
    "ResolvedDirective",
    "StandingDirective",
    "StandingDirectivePayload",
    "golden_rule_cadence_seconds",
    "override_prompt_name",
    "pr_board_cadence_seconds",
    "resolve_standing_directives",
    "todo_consolidate_cadence_seconds",
]
