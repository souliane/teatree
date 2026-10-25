"""Who acts next on an open pull request, and what they do: one decision for the tick and the board."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.review.mr_ci_state import GREEN_STATUSES

if TYPE_CHECKING:
    from teatree.core.review.pr_inventory import PrInventoryEntry

GROUPS = ("Needs you", "Waiting on colleagues", "Factory working", "Ready to merge or merging", "Blocked")
UNKNOWN = "unknown"
APPROVED = "approved"
AWAITING_APPROVAL = "awaiting approval"
FIX_CI = "Fix failing CI"
UPDATE_BRANCH = "Update PR branch against main"

_RED_CI = frozenset({"red", "failed", "failure", "error", "canceled", "cancelled"})
_GREEN_CI = GREEN_STATUSES | {"green"}
_PENDING_CI = frozenset({"pending", "running"})
_BLOCKING_DECISIONS = frozenset({"flag_conflict", "needs_branch_update", "required_checks_indeterminate"})
_BLOCKING_REASONS = frozenset({"draft", "changes_requested", "evaluation_failed"})
_OWNER_REASONS = frozenset(
    {"mergeable_awaiting_review", "fork_requires_human_approval", "untrusted_author_public_repo"}
)
_AUTHORIZATION_REASONS = frozenset({"no_clear_for_head", "mergeable_awaiting_review"})
_PAST_CI_GATE_REASONS = _AUTHORIZATION_REASONS | {
    "clear_present_unusable",
    "solo_overlay_no_review",
    "hold_at_head",
    "contested_hold_at_head",
}
_APPROVAL_RATIO = re.compile(r"(\d+)/(\d+)")
_UNRESOLVED_THREADS = re.compile(r"([1-9]\d*) unresolved")


@dataclass(frozen=True, slots=True)
class PrNextAction:
    group: str
    actor: str
    text: str


@dataclass(frozen=True, slots=True)
class PrActionFacts:
    entry: "PrInventoryEntry | None"
    working: str
    failure: str
    review_requested: bool
    question: str
    ci: str
    approvals: str
    threads: str
    threads_blocking: bool
    draft: bool
    verified: bool
    owned: bool | None


def approvals_label(approvals_left: int) -> str:
    return APPROVED if approvals_left == 0 else AWAITING_APPROVAL


def threads_label(unresolved: int) -> str:
    return f"{unresolved} unresolved"


def approvals_text(label: str, count: int | None, required: int | None) -> str:
    if count is None and required is None:
        return label
    return f"{UNKNOWN if count is None else count}/{UNKNOWN if required is None else required} ({label})"


def approvals_met(approvals: str) -> bool | None:
    if approvals == APPROVED:
        return True
    if approvals == AWAITING_APPROVAL:
        return False
    if ratio := _APPROVAL_RATIO.fullmatch(approvals):
        return int(ratio[1]) >= int(ratio[2])
    return None


def sweep_next_task(reason: str, decision: str, *, behind_main: bool) -> str:
    if reason == "no_clear_for_head":
        return UPDATE_BRANCH if behind_main else "Obtain independent review and merge authorization"
    actions = {
        "draft": "Finish draft and request review",
        "changes_requested": "Address requested changes",
        "ci_red": FIX_CI,
        "ci_pending": "Wait for CI to finish",
        "required_checks_indeterminate": "Inspect required checks",
        "conflict": "Resolve merge conflict",
        "solo_overlay_no_review": "Run independent review",
        "clear_present_unusable": "Refresh merge authorization for current head",
        "mergeable_awaiting_review": "Request independent review",
    }
    return actions.get(reason, f"Inspect PR blocker: {(reason or decision).replace('_', ' ')}")


def my_pr_next_task(kind: str, ci: str) -> str:
    tasks = {
        "my_pr.failed": FIX_CI,
        "my_pr.findings_to_fix": "Fix review findings",
        "my_pr.draft_notes": "Await author response to review findings",
        "my_pr.manual_action": "Complete manual CI action",
    }
    return tasks.get(kind, "Review or merge PR" if ci in _GREEN_CI else "Inspect CI and advance PR")


def ci_label(reason: str, *, failing_required: bool) -> str:
    if failing_required or reason == "ci_red":
        return "red"
    if reason == "ci_pending":
        return "pending"
    return "green" if reason in _PAST_CI_GATE_REASONS else UNKNOWN


def entry_blocker(entry: "PrInventoryEntry") -> str:
    if entry.ci.lower() in _RED_CI:
        return FIX_CI
    if entry.decision in _BLOCKING_DECISIONS or entry.reason in _BLOCKING_REASONS:
        return entry.next_task
    return ""


def stall_cause(entry: "PrInventoryEntry") -> str:
    """What the factory can repair by coding; a draft park or a sweep fault is a blocker, never a stall."""
    if entry.ci.lower() in _RED_CI:
        return FIX_CI
    return entry.next_task if entry.decision == "flag_conflict" or entry.reason == "changes_requested" else ""


def _blocked_reason(facts: PrActionFacts) -> str:
    if facts.failure:
        return f"Fix failed task: {facts.failure[:160]}"
    if facts.entry is not None:
        if blocker := entry_blocker(facts.entry):
            return blocker
    elif facts.ci.lower() in _RED_CI:
        return FIX_CI
    if (facts.threads_blocking or approvals_met(facts.approvals) is True) and (
        unresolved := _UNRESOLVED_THREADS.fullmatch(facts.threads)
    ):
        return f"Resolve {unresolved[1]} unresolved thread(s)"
    return ""


def _authorization_action(entry: "PrInventoryEntry") -> PrNextAction:
    if entry.reason in _OWNER_REASONS:
        return PrNextAction("Needs you", "You", entry.next_task)
    if entry.next_task == UPDATE_BRANCH:
        return PrNextAction("Blocked", "Factory", entry.next_task)
    return PrNextAction("Waiting on colleagues", "Reviewer", entry.next_task)


def _owner_waiting(facts: PrActionFacts) -> PrNextAction | None:
    return PrNextAction("Needs you", "You", facts.question) if facts.question else None


def _factory_task(facts: PrActionFacts) -> PrNextAction | None:
    return PrNextAction("Factory working", "Factory", facts.working) if facts.working else None


def _foreign_pr(facts: PrActionFacts) -> PrNextAction | None:
    if facts.owned is not False or facts.entry is None or facts.failure:
        return None
    if reason := _blocked_reason(facts):
        return PrNextAction("Blocked", "PR author", reason)
    if facts.ci.lower() in _PENDING_CI:
        return PrNextAction("Factory working", "CI", "Wait for CI to finish")
    return PrNextAction("Waiting on colleagues", "PR author", facts.entry.next_task or "Await PR author")


def _blocked(facts: PrActionFacts) -> PrNextAction | None:
    reason = _blocked_reason(facts)
    return PrNextAction("Blocked", "Factory", reason) if reason else None


def _ready(facts: PrActionFacts) -> PrNextAction | None:
    if facts.entry is not None and facts.entry.reason:
        return None
    if (
        approvals_met(facts.approvals) is not True
        or facts.ci.lower() not in _GREEN_CI
        or facts.draft
        or facts.threads != "0 unresolved"
    ):
        return None
    if facts.verified:
        return PrNextAction("Ready to merge or merging", "Factory", "Merge through the ticket keystone")
    return PrNextAction("Blocked", "Factory", "Forge facts not verified by a recent tick; waiting for the next read")


def _authorization(facts: PrActionFacts) -> PrNextAction | None:
    if facts.entry is None or facts.entry.reason not in _OWNER_REASONS | _AUTHORIZATION_REASONS:
        return None
    return _authorization_action(facts.entry)


def _awaiting_review(facts: PrActionFacts) -> PrNextAction | None:
    if approvals_met(facts.approvals) is False:
        return PrNextAction("Waiting on colleagues", "Reviewer", f"Await approvals ({facts.approvals})")
    if facts.review_requested:
        return PrNextAction("Waiting on colleagues", "Reviewer", "Review requested")
    return None


def _unreadable_approvals(facts: PrActionFacts) -> PrNextAction | None:
    if facts.entry is None or facts.entry.reason or facts.ci.lower() not in _GREEN_CI:
        return None
    if approvals_met(facts.approvals) is not None:
        return None
    return PrNextAction("Blocked", "Factory", "Approvals unreadable; check the forge")


def _unreadable_threads(facts: PrActionFacts) -> PrNextAction | None:
    if (
        facts.entry is None
        or facts.entry.reason
        or facts.ci.lower() not in _GREEN_CI
        or approvals_met(facts.approvals) is not True
        or facts.threads == "0 unresolved"
    ):
        return None
    return PrNextAction("Blocked", "Factory", "Review threads unreadable; check the forge")


def _observed_next_task(facts: PrActionFacts) -> PrNextAction | None:
    if facts.entry is None or not facts.entry.next_task:
        return None
    group = "Factory working" if facts.ci.lower() in _PENDING_CI else "Blocked"
    return PrNextAction(group, "Factory", facts.entry.next_task)


_RULES = (
    _owner_waiting,
    _factory_task,
    _foreign_pr,
    _blocked,
    _ready,
    _authorization,
    _unreadable_threads,
    _awaiting_review,
    _unreadable_approvals,
    _observed_next_task,
)


def decide(facts: PrActionFacts) -> PrNextAction:
    for rule in _RULES:
        if (outcome := rule(facts)) is not None:
            return outcome
    if facts.ci.lower() == UNKNOWN:
        return PrNextAction("Blocked", "Factory", "PR state unreadable; inspect live checks and approvals")
    return PrNextAction("Factory working", "Factory", "Inspect pull request")
