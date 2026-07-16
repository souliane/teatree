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
        "is_terminal",
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

# The FSM state graph as it stands before the split — {transition: (sources, targets)}.
# Behaviour-preserving means this map is byte-identical afterwards.
_FSM_GRAPH: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "code": (("planned",), ("coded",)),
    "code_direct": (("not_started", "scoped", "started"), ("coded",)),
    "ignore": (
        (
            "coded",
            "in_review",
            "merged",
            "not_started",
            "planned",
            "retrospected",
            "reviewed",
            "scoped",
            "shipped",
            "started",
            "tested",
        ),
        ("ignored",),
    ),
    "mark_delivered": (("retrospected",), ("delivered",)),
    "mark_merged": (("in_review", "merged"), ("merged",)),
    "mark_review_no_action": (
        ("coded", "delivered", "not_started", "planned", "reviewed", "scoped", "started", "tested"),
        ("delivered",),
    ),
    "mark_reviewed_externally": (
        ("coded", "not_started", "planned", "reviewed", "scoped", "started", "tested"),
        ("delivered",),
    ),
    "plan": (("started",), ("planned",)),
    "reconcile_merged": (
        (
            "coded",
            "in_review",
            "merged",
            "not_started",
            "planned",
            "reviewed",
            "scoped",
            "shipped",
            "started",
            "tested",
        ),
        ("merged",),
    ),
    "reconcile_reviewed": (
        ("coded", "in_review", "not_started", "planned", "retrospected", "reviewed", "scoped", "started", "tested"),
        ("reviewed",),
    ),
    "reopen": (("in_review", "merged", "retrospected", "shipped"), ("started",)),
    "request_review": (("shipped",), ("in_review",)),
    "retrospect": (("merged", "retrospected"), ("retrospected",)),
    "review": (("tested",), ("reviewed",)),
    "rework": (("coded", "reviewed", "tested"), ("started",)),
    "scope": (("not_started",), ("scoped",)),
    "ship": (("reviewed", "shipped"), ("shipped",)),
    "start": (("scoped", "started"), ("started",)),
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
