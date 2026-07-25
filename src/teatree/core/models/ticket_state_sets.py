from teatree.core.models.ticket_data import TicketFacet


class TicketStateSetsModel(TicketFacet):
    """The canonical ticket state-set classmethods — the SSOT the loop reads.

    Every scanner, manager, and sweep that needs "which states count as done /
    in-flight / completable / merged" reads these instead of re-hand-rolling the
    set (the raw-string drift class behind #798/#799/#808). Each returns an
    immutable ``frozenset`` of ``State`` values; membership is pinned by
    ``tests/teatree_core/models/test_ticket.py::TestTicketStateSets``.
    """

    class Meta:
        abstract = True

    @classmethod
    def marker_release_states(cls) -> frozenset[str]:
        """Terminal-done states that free markers and trigger worktree teardown.

        ``_TERMINAL_STATES`` minus SHIPPED (its PR is still open). Shared by the
        teardown/marker signal and the #3275 reconciler; REVIEW_POSTED is the
        reviewer terminal (marker release is a no-op for reviewer tickets).
        """
        return frozenset({cls.State.MERGED, cls.State.DELIVERED, cls.State.REVIEW_POSTED, cls.State.IGNORED})

    @classmethod
    def in_flight_excluded_states(cls) -> frozenset[str]:
        """States that drop a ticket OUT of the in-flight working set.

        ``marker_release_states()`` minus MERGED: a merged-but-not-yet-delivered
        ticket's PR has landed, but the ticket is still in flight (retro/delivery
        pending) so it stays on the board. The in-flight queryset, the dashboard
        worktrees panel, and both active-ticket scanners (primary ORM + external
        SQLite) ``exclude(state__in=...)`` on exactly this set.
        """
        return frozenset({cls.State.DELIVERED, cls.State.REVIEW_POSTED, cls.State.IGNORED})

    @classmethod
    def completable_states(cls) -> frozenset[str]:
        """Post-ship states a completion sweep may advance toward DELIVERED.

        SHIPPED (PR open), IN_REVIEW (review requested), MERGED (PR landed) — the
        states worth polling ``is_issue_done()`` on before driving the ticket to
        delivered. Read by the completion scanner and the ``sync-completions`` sweep.
        """
        return frozenset({cls.State.SHIPPED, cls.State.IN_REVIEW, cls.State.MERGED})

    @classmethod
    def merged_states(cls) -> frozenset[str]:
        """States that mean the ticket's PR has landed (merged-or-past).

        MERGED (just landed) plus the RETROSPECTED/DELIVERED post-merge lifecycle.
        The outer- and directive-loop liveness guards and the issue-disposition
        dedup read this to tell a landed ticket from one whose PR is still open.
        Narrower than an "after-merge trigger" set that excludes RETROSPECTED —
        that lives at its own call site on purpose.
        """
        return frozenset({cls.State.MERGED, cls.State.RETROSPECTED, cls.State.DELIVERED})
