"""The CLI-allowed ticket transition names.

Split out of ``ticket.py`` (the cap-bound command god-module) as a pure data
constant the ``transition`` command validates against — the FSM owns the actual
transitions; this is only the CLI's allow-list of names it will dispatch.
"""

ALLOWED_TRANSITIONS = {
    "scope",
    "start",
    "plan",
    "code",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
    # #1077: reviewer concludes an external review with no postable/
    # approvable action — terminal disposition for the reviewing task.
    "mark_review_no_action",
    # #1118: phase-driven catch-up to REVIEWED. The FSM exposes it via
    # ``get_available_FIELD_transitions`` from every non-terminal state
    # (#808); the CLI must mirror the FSM-table surface so a ticket
    # stranded at ``in_review`` after a failed ship can be reconciled
    # without a code-level workaround.
    "reconcile_reviewed",
    # Abandon/neutralize a mis-adopted or stray ticket: ``ignore`` drives the
    # reversible terminal IGNORED state (its body only stamps ``ignored_from``;
    # it enqueues no teardown/ship task and posts nothing to the forge), and
    # ``unignore`` restores the pre-abandon state. Both are FSM-model methods
    # the CLI merely refused to dispatch.
    "ignore",
    "unignore",
}
