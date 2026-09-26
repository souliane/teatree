"""Structural guards for the ``Ticket`` god-object split (burndown Unit 5).

``Ticket`` breached the repo's ``max-public-methods=25`` ceiling (47 public
methods) and silenced it with a class-level PLR0904 suppression. The split moves
cohesive instance-behaviour clusters onto composed abstract-model facets so the
concrete ``Ticket`` body drops under the ceiling — WITHOUT changing the public API
(every method stays reachable as ``ticket.foo()`` via the facets) or the FSM state
graph. These tests pin all three properties.
"""

import inspect

from teatree.core.models.ticket import Ticket

# The public surface consumers call on a ``Ticket`` before the split — every name
# must stay reachable on the concrete class afterwards (via the composed facets),
# so no consumer call site breaks. Frozen: dropping one is an API regression.
_PUBLIC_API: frozenset[str] = frozenset(
    {
        "aggregate_phase_records",
        "append_context",
        "apply_inferred_overlay",
        "artifacts",
        "code",
        "ensure_session",
        "find_phase_session",
        "has_active_work",
        "has_dispatchable_overlay",
        "has_shippable_diff",
        "ignore",
        "is_settled",
        "mark_delivered",
        "mark_merged",
        "mark_remote_missing",
        "mark_review_no_action",
        "mark_reviewed_externally",
        "may_expedite",
        "merge_extra",
        "plan",
        "reconcile_merged",
        "reconcile_overlay",
        "reconcile_reviewed",
        "record_anti_vacuity_attestation",
        "record_review_context",
        "record_review_skill_run",
        "reopen",
        "reopen_for_followup",
        "request_review",
        "resolve_phase_session",
        "retrospect",
        "review",
        "review_context_satisfied",
        "rework",
        "save",
        "schedule_coding",
        "schedule_planning",
        "schedule_review",
        "schedule_review_in_session",
        "schedule_shipping",
        "schedule_testing",
        "scope",
        "ship",
        "start",
        "test",
        "ticket_number",
        "unignore",
    }
)

# The full FSM state graph — {transition: (sources, targets)}. The god-object
# split had to leave it byte-identical; every later edge is a deliberate,
# reviewed addition pinned here (e.g. reopen_for_followup, #3327; reopen's
# delivered source, #4152).
_FSM_GRAPH: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "code": (("plan_recorded",), ("coded",)),
    "code_direct": (("not_started", "scoped", "work_started"), ("coded",)),
    "ignore": (
        (
            "coded",
            "merged",
            "not_started",
            "plan_recorded",
            "pr_opened",
            "retro_recorded",
            "review_requested",
            "scoped",
            "self_reviewed",
            "tested",
            "work_started",
        ),
        ("ignored",),
    ),
    "mark_delivered": (("retro_recorded",), ("delivered",)),
    "mark_merged": (("merged", "review_requested"), ("merged",)),
    "mark_review_no_action": (
        (
            "coded",
            "not_started",
            "plan_recorded",
            "review_delivered",
            "scoped",
            "self_reviewed",
            "tested",
            "work_started",
        ),
        ("review_delivered",),
    ),
    # ``delivered`` is a deliberate SELF-loop source: a re-review at a new head
    # SHA lands on a ticket the previous pass already delivered, and the
    # transition must be able to re-stamp ``reviewed_sha``/``last_review_state``
    # there. Same shape as the sibling ``mark_review_no_action`` (#1431).
    "mark_reviewed_externally": (
        (
            "coded",
            "not_started",
            "plan_recorded",
            "review_delivered",
            "scoped",
            "self_reviewed",
            "tested",
            "work_started",
        ),
        ("review_delivered",),
    ),
    "plan": (("work_started",), ("plan_recorded",)),
    "reconcile_merged": (
        (
            "coded",
            "merged",
            "not_started",
            "plan_recorded",
            "pr_opened",
            "review_requested",
            "scoped",
            "self_reviewed",
            "tested",
            "work_started",
        ),
        ("merged",),
    ),
    "reconcile_reviewed": (
        (
            "coded",
            "not_started",
            "plan_recorded",
            "retro_recorded",
            "review_requested",
            "scoped",
            "self_reviewed",
            "tested",
            "work_started",
        ),
        ("self_reviewed",),
    ),
    "reopen": (("delivered", "merged", "pr_opened", "retro_recorded", "review_requested"), ("work_started",)),
    "reopen_for_followup": (("delivered", "merged"), ("self_reviewed",)),
    "request_review": (("pr_opened",), ("review_requested",)),
    "retrospect": (("merged", "retro_recorded"), ("retro_recorded",)),
    "review": (("tested",), ("self_reviewed",)),
    "rework": (("coded", "self_reviewed", "tested"), ("work_started",)),
    "scope": (("not_started",), ("scoped",)),
    "ship": (("pr_opened", "self_reviewed"), ("pr_opened",)),
    "start": (("scoped", "work_started"), ("work_started",)),
    "test": (("coded",), ("tested",)),
}


def _own_public_members(cls: type) -> set[str]:
    """Public methods/properties defined directly in *cls*'s body (the PLR0904 shape).

    Mirrors ruff's ``too-many-public-methods`` count: names in the class ``__dict__``
    that are callable/property and neither private nor dunder. Inherited members
    (from the composed facets) are excluded — exactly what the ceiling counts.
    """
    members: set[str] = set()
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(value, (staticmethod, classmethod, property)) or inspect.isfunction(value):
            members.add(name)
    return members


def _fsm_graph(cls: type) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    graph: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for name, value in vars(cls).items():
        fsm = getattr(value, "_django_fsm", None)
        if fsm is None:
            continue
        sources = tuple(sorted(str(s) for s in fsm.transitions))
        targets = tuple(sorted({str(t.target) for t in fsm.transitions.values()}))
        graph[name] = (sources, targets)
    return graph


class TestGodObjectShrink:
    def test_own_public_method_count_under_ceiling(self) -> None:
        # pyproject sets lint.pylint.max-public-methods = 25; the concrete Ticket
        # body must live under it with the facets carrying the rest.
        assert len(_own_public_members(Ticket)) <= 25


class TestPublicApiPreserved:
    def test_every_pre_split_public_method_is_still_reachable(self) -> None:
        missing = sorted(name for name in _PUBLIC_API if not hasattr(Ticket, name))
        assert missing == []


class TestFsmGraphUnchanged:
    def test_transition_sources_and_targets_are_unchanged(self) -> None:
        assert _fsm_graph(Ticket) == _FSM_GRAPH
