"""The CLI-allowed ticket transition names — the single source of truth.

Split out of ``ticket.py`` (the cap-bound command god-module) as pure data the
``transition`` command validates against AND derives its ``--help`` text from, so
the allow-list and the documented list can never drift. The FSM owns the actual
transitions; this list must name every one of them, plus the handful of non-FSM
state mutators the CLI also dispatches — both directions are pinned by
``tests/teatree_core/management/commands/test_transition_names.py``, which derives
the expected set from the ``Ticket.state`` field rather than restating it.
"""

# Ordered in FSM-ladder order so the derived help reads top-to-bottom; the
# validated allow-list below is this same set.
ALLOWED_TRANSITION_NAMES: tuple[str, ...] = (
    "scope",
    "start",
    "plan",
    "code",
    "code_direct",
    "test",
    "review",
    "ship",
    "request_review",
    "mark_merged",
    "retrospect",
    "mark_delivered",
    "rework",
    "reopen",
    "reopen_for_followup",
    # #1077: reviewer concludes an external review with no postable/
    # approvable action — terminal disposition for the reviewing task.
    "mark_review_no_action",
    "mark_reviewed_externally",
    # #1118: phase-driven catch-up to REVIEWED. The FSM exposes it via
    # ``get_available_FIELD_transitions`` from every non-terminal state
    # (#808); the CLI must mirror the FSM-table surface so a ticket
    # stranded at ``in_review`` after a failed ship can be reconciled
    # without a code-level workaround.
    "reconcile_reviewed",
    "reconcile_merged",
    # Abandon/neutralize a mis-adopted or stray ticket: ``ignore`` drives the
    # reversible terminal IGNORED state (its body stamps ``ignored_from`` and posts
    # nothing to the forge; teardown IS enqueued, keyed on the target state by #808,
    # and the reaper's analyze-before-wipe keeps unsynced work), and ``unignore``
    # restores the pre-abandon state. ``unignore`` is the one entry here that is not
    # an FSM transition: restoring a *stored* previous state has no static ``target``,
    # so it guards itself instead of carrying ``@transition``.
    "ignore",
    "unignore",
)

ALLOWED_TRANSITIONS = frozenset(ALLOWED_TRANSITION_NAMES)

TRANSITION_HELP = (
    "Transition a ticket to a new state. Allowed transition names: " + ", ".join(ALLOWED_TRANSITION_NAMES) + "."
)
