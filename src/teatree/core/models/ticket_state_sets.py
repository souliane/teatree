from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket_data import TicketFacet

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    """Outcome of one :meth:`TicketStateSetsModel.advance_to_delivered` walk.

    ``from_state`` is the pre-walk state; ``to_state`` is where the ticket
    actually landed. Each step commits in its own ``atomic()``, so a mid-chain
    refusal leaves the earlier steps persisted — ``to_state`` reflects that
    partial progress, not the starting state. ``error`` carries the FSM/gate
    refusal message when the walk stopped short, else ``None``.
    """

    from_state: str
    to_state: str
    error: str | None = None

    @property
    def refused(self) -> bool:
        return self.error is not None

    @property
    def advanced(self) -> bool:
        return self.to_state != self.from_state


class TicketStateSetsModel(TicketFacet):
    """The completion facet — the canonical post-ship state-sets and the walk over them.

    Every scanner, manager, and sweep that needs "which states count as done /
    in-flight / completable / merged" reads the classmethods here instead of
    re-hand-rolling the set (the raw-string drift class behind #798/#799/#808).
    Each returns an immutable ``frozenset`` of ``State`` values; membership is
    pinned by ``tests/teatree_core/models/test_ticket.py::TestTicketStateSets``.

    ``advance_to_delivered`` is the single transactional walk over
    ``completable_states()`` toward DELIVERED, shared by the ``sync-completions``
    sweep and the loop's mechanical ``complete_ticket`` so both get identical
    atomic-per-step + refusal-safe semantics.
    """

    class Meta:
        abstract = True

    # The post-ship walk as ``(guard_state, transition_name)`` steps.
    # request_review: SHIPPED → IN_REVIEW; mark_merged: IN_REVIEW → MERGED;
    # retrospect: MERGED → RETROSPECTED (the retro worker then drives mark_delivered).
    _ADVANCE_STEPS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("shipped", "request_review"),
        ("in_review", "mark_merged"),
        ("merged", "retrospect"),
    )

    def advance_to_delivered(self: "Ticket") -> AdvanceResult:
        """Walk the ticket through the remaining post-ship FSM transitions.

        Each step runs in its own ``transaction.atomic()``: a successful step
        commits, a refused step rolls back only itself and stops the walk. A
        django-fsm ``TransitionNotAllowed`` or a gate's ``InvalidTransitionError``
        (e.g. the merge-evidence gate on ``mark_merged``) is captured in the
        returned :class:`AdvanceResult`, never raised — so the whole-table sweep
        keeps going and the loop tick never wedges on a partial commit.
        """
        from_state = self.state
        for guard_state, transition_name in self._ADVANCE_STEPS:
            if self.state != guard_state:
                continue
            try:
                with transaction.atomic():
                    getattr(self, transition_name)()
                    self.save()
            except (TransitionNotAllowed, InvalidTransitionError) as exc:
                return AdvanceResult(from_state=from_state, to_state=self.state, error=str(exc))
        return AdvanceResult(from_state=from_state, to_state=self.state)

    @classmethod
    def state_index(cls, state: str) -> int:
        """Position of *state* in the ``State`` declaration order."""
        return [s.value for s in cls.State].index(state)

    @classmethod
    def state_advances(cls, current: str, candidate: str) -> bool:
        """True iff writing *candidate* over *current* moves the ticket forward.

        The single ordering every external-sync writer compares against. A
        tracker/board tells us where IT thinks the ticket is; that is an
        advance-only signal, never a rewind — a board column or an inferred PR
        state that reads behind the ticket's own FSM would otherwise reset live
        work (an in-review ticket back to not-started) and re-arm the loop's
        scanners against already-delivered work.
        """
        return cls.state_index(candidate) > cls.state_index(current)

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
