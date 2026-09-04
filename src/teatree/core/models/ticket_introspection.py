from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Max

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models.ticket_data import TicketFacet
from teatree.core.models.ticket_number import derive_issue_number
from teatree.core.models.ticket_worktree_checks import worktree_has_commits_ahead
from teatree.core.models.types import SlackAnswerContext
from teatree.utils.url_slug import is_synthetic_loop_umbrella_url

if TYPE_CHECKING:
    from teatree.core.managers import SessionQuerySet, TaskQuerySet
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.ticket_artifacts import PortResolver, TicketArtifacts
    from teatree.core.models.worktree import Worktree


class TicketIntrospectionModel(TicketFacet):
    """Read-only identity, liveness, and diff/artifact introspection over the ticket and its related rows."""

    if TYPE_CHECKING:
        # Reverse accessors Django synthesises at class-prep time from the
        # ``related_name`` on ``Task.ticket`` / ``Session.ticket`` — invisible to a
        # static checker, so declared here. Annotation-only; never evaluated at
        # runtime. Typed as the QUERYSET because each manager is built dynamically
        # via ``Manager.from_queryset(...)``, whose name holds a variable and so
        # cannot appear in a type expression.
        tasks: "TaskQuerySet"
        sessions: "SessionQuerySet"

    class Meta:
        abstract = True

    def has_active_work(self) -> bool:
        """True iff this ticket has a LIVE session or an active (pending/claimed) task.

        The single owner of the ticket-liveness rule the reapers and the relocate
        command consult — a busy ticket must never be torn down.

        A session counts as live while it is open AND its last recorded activity
        is inside ``session_stale_after_hours`` (:meth:`SessionQuerySet.live`).
        The bound is what lets the reapers converge; it cannot mask real in-flight
        work, because the task half below carries no time bound at all.
        """
        if self.sessions.live().exists():  # Django reverse FK
            return True
        # apps.get_model, not a direct import: task.py imports ticket.py at module scope (real cycle).
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        return self.tasks.filter(status__in=task_model.Status.active()).exists()  # Django reverse FK

    def newest_task_was_cancelled(self) -> bool:
        """True when a human cancelled this ticket's NEWEST task (#4105).

        The ONE reading of "an operator said stop", shared by the marker reconciler
        (which releases such a claim as DECLINED rather than the re-claimable
        ABANDONED) and the stuck-ticket drain (which leaves the ticket alone). Two
        opinions here would let one actor undo the other's honoured decision.

        The NEWEST task decides, so anything the pipeline did afterwards — a re-queue,
        a later failure — is the "something changed" that puts the ticket back in play.
        ``SUPERSEDED`` is deliberately not included: that is rework the factory itself
        initiated, so the ticket stays claimable.

        The newest task is read with ``Max``, not ``order_by("-created_at")``:
        ``Task.created_at`` is nullable, and DESC ordering puts NULLs FIRST on
        PostgreSQL (last on SQLite), so one null-stamped row would decide this.
        """
        newest = self.tasks.aggregate(latest=Max("created_at"))["latest"]  # Django reverse FK
        if newest is None:
            return False
        return self.tasks.filter(created_at=newest, failure_kind=FailureKind.CANCELLED).exists()  # Django reverse FK

    @property
    def is_terminal(self) -> bool:
        """True when the ticket is in a genuinely terminal/abandoned state (SHIPPED/MERGED/DELIVERED/IGNORED)."""
        return self.state in self._TERMINAL_STATES

    @classmethod
    def phase_producing_state(cls, state: str) -> str:
        """The phase whose successful output IS *state*, or ``""`` for an off-ladder state.

        The inverse of :attr:`_PHASE_PRODUCES_STATE`, read by the cycle-time layer to
        name the phase that occupied a ``from_state -> to_state`` span. Derived rather
        than re-listed so a phase added to the forward map cannot go unnamed here.
        """
        return next((phase for phase, produces in cls._PHASE_PRODUCES_STATE.items() if produces == state), "")

    def has_completed_phase(self, phase: str) -> bool:
        """True when the FSM state has already reached the state *phase* produces.

        A FAILED task for such a phase is SUPERSEDED: the ticket's own FSM advanced
        past that phase's output (an earlier interrupted run left the dead row), so
        re-dispatching or escalating it only floods the away-mode DeferredQuestion
        queue with a question that is already answered by the ticket's state. The
        transient-requeue sweep retires such tasks silently instead of asking the
        owner. An unknown phase, or a state off the linear work ladder
        (IN_REVIEW/RETROSPECTED/…), is conservatively treated as NOT completed —
        the safe default that escalates rather than silently drops a live task.
        """
        produces = self._PHASE_PRODUCES_STATE.get(normalize_phase(phase))
        order = self._WORK_STATE_ORDER
        if produces is None or self.state not in order:
            return False
        return order.index(self.state) >= order.index(produces)

    def may_expedite(self) -> bool:
        """True iff this ticket may carry a human-authorized PENDING-checks waiver (PR-07).

        The flag alone grants NO merge bypass — it only makes the per-CLEAR,
        SHA-bound waiver ISSUABLE (§17.4.3 / ``MergeClear.expedite_pending_waived_by``).
        """
        return self.expedited

    @property
    def ticket_number(self) -> str:
        """Forge issue number derived from ``issue_url``, else the pk (see ``derive_issue_number``).

        Denormalized into the indexed ``issue_number`` column for O(1) resolves;
        this property keeps the pk fallback for rows carrying no forge number.
        """
        return derive_issue_number(self.issue_url) or str(self.pk)

    def is_admissible(self) -> bool:
        """Whether intake could ever find this row — a real forge issue with a name (#4527).

        Intake discovers candidates from forge queries, so a row whose ``issue_url``
        is blank, a synthetic ``auto:``/loop anchor, or whose remote is gone can
        never be admitted, claimed, or found again. This is the single predicate a
        surface consults before promising the owner that a ticket tracks their
        request: announcing an inadmissible row converts a dropped request into one
        the owner believes is tracked, which is strictly worse than no row at all.
        """
        url = self.issue_url
        if not url or url.startswith("auto:") or is_synthetic_loop_umbrella_url(url):
            return False
        return bool(self.short_description) and not self.remote_missing

    def recorded_request(self) -> str:
        """All an unfindable row still holds — the request text, else its card label (#4527).

        A row intake cannot reach is the only surviving record of what someone asked
        for, so every surface that reports one has to show that text or the report
        names a number nobody can act on.
        """
        recorded = str(self._slack_answer().get("question") or "")
        return recorded or self.short_description or "(no recorded text)"

    def work_placed_elsewhere(self) -> bool:
        """Whether this conversation row already recorded the findable row it became (#4527).

        A Slack lane's bookkeeping row is non-admissible by design, so admissibility alone
        cannot tell a dropped request from a handled one — this stamp is the difference.
        """
        return bool(self._slack_answer().get("work_issue_url"))

    def _slack_answer(self) -> SlackAnswerContext:
        extra = self.extra if isinstance(self.extra, dict) else {}
        origin = extra.get("slack_answer")
        return cast("SlackAnswerContext", origin) if isinstance(origin, dict) else SlackAnswerContext()

    def has_shippable_diff(self) -> bool:
        """Return True iff at least one worktree has commits ahead of its base branch.

        Used by ``review()`` to skip auto-scheduling shipping when there is
        nothing to ship — typically meta-tracker tickets whose work already
        landed via sibling PRs. Manual ``schedule_shipping()`` callers are not
        gated.
        """
        worktree_model = cast("type[Worktree]", apps.get_model("core", "Worktree"))
        return any(worktree_has_commits_ahead(wt) for wt in worktree_model.objects.filter(ticket=self))

    def artifacts(self: "Ticket", *, port_resolver: "PortResolver | None" = None) -> "TicketArtifacts":
        """Read-only artifact-discovery aggregation (#273) — see ``ticket_artifacts``."""
        from teatree.core.models.ticket_artifacts import collect_ticket_artifacts  # noqa: PLC0415 — import cycle

        return collect_ticket_artifacts(self, port_resolver=port_resolver)
