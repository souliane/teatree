"""The ONE shared registry of Bash-shaped hard-deny predicates (#2, souliane/teatree).

A single source of truth for the **Bash-shaped** shell-command refusals that BOTH
lanes must enforce identically — the pure ``(command) -> reason`` matchers,
consulted by the cold PreToolUse subprocess (``hooks/scripts/hook_router`` and its
sibling gates) AND the Lane-B ``pydantic_ai`` shell
(:func:`teatree.agents.lane_b.gating.hard_deny_reason`, which iterates this
registry). It is NOT the whole shell-refusal set: the cwd-scoped
main-clone-mutation gate and the payload-scoped privacy/banned-term gate are
shell-command refusals too, but they carry per-call context and live outside this
registry — Lane B composes all three. Before this registry the two diverged — Lane B's ``hard_deny_reason``
checked only the main-clone + privacy gates, so a raw ``gh pr merge`` /
``git push --no-verify`` / secret-print / raw-review-post / reviewer-assign /
raw-pid-kill was reachable under ``agent_harness=pydantic_ai`` with NO MergeClear
or CI verification (the "Lane-B bypass" class). Registering every Bash-shaped deny
predicate here, iterated by both consumers, closes that class by construction.

Each predicate is a PURE ``(command) -> reason-or-None`` matcher living in its own
:mod:`teatree.hooks` leaf (stdlib-only, importable by Lane B and by the cold
subprocess alike). The predicates carry NO cwd/kill-switch/carve-out context — that
per-gate context (the raw-merge unmanaged-repo carve-out, the reviewer-ok token,
the config kill-switches) stays in the individual PreToolUse guards, which layer it
ON TOP of the same pure detectors. The deny-corpus parity test
(``tests/teatree_agents/lane_b/test_parity.py``) feeds every Lane-A deny fixture
through :func:`hard_deny_reason` and asserts identical refusals, so a future
divergence between a leaf and its Lane-A guard fails CI.
"""

from collections.abc import Callable

from teatree.hooks import (
    git_bypass_detect,
    raw_merge_detect,
    raw_review_post_detect,
    safe_kill_detect,
    secret_file_print_detect,
    self_reviewer_assign_detect,
)

#: A pure Bash-shaped deny predicate: the refusal reason for a command, or ``None``.
HardDenyPredicate = Callable[[str], str | None]


def _raw_pid_kill_deny_reason(command: str) -> str | None:
    """The raw-pid-kill deny reason for *command*, or ``None`` — wraps the detection leaf."""
    detection = safe_kill_detect.detect_raw_pid_kill(command)
    return detection.message if detection.is_raw_pid_kill else None


#: The SSOT list of ``(name, predicate)`` pairs iterated by BOTH the cold
#: PreToolUse guards (each delegating to the same leaf) and Lane B's
#: :func:`~teatree.agents.lane_b.gating.hard_deny_reason`. Order is deny priority:
#: the first predicate to return a reason wins.
HARD_DENY_PREDICATES: tuple[tuple[str, HardDenyPredicate], ...] = (
    ("raw_merge", raw_merge_detect.raw_merge_deny_reason),
    ("git_bypass", git_bypass_detect.git_bypass_deny_reason),
    ("secret_file_print", secret_file_print_detect.secret_print_deny_reason),
    ("raw_review_post", raw_review_post_detect.raw_review_deny_reason),
    ("self_reviewer_assign", self_reviewer_assign_detect.reviewer_assign_deny_reason),
    ("raw_pid_kill", _raw_pid_kill_deny_reason),
)


def hard_deny_reason(command: str) -> str | None:
    """Return the first registered predicate's refusal reason for *command*, or ``None``.

    The shared iteration both lanes run: a command denied by any Bash-shaped
    predicate here is refused identically on Lane A and Lane B. An empty command
    (a non-command tool call) is never a shell egress, so it is allowed.
    """
    if not command:
        return None
    for _name, predicate in HARD_DENY_PREDICATES:
        reason = predicate(command)
        if reason is not None:
            return reason
    return None


__all__ = ["HARD_DENY_PREDICATES", "HardDenyPredicate", "hard_deny_reason"]
