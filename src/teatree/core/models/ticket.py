import logging
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django_fsm import FSMField, transition

from teatree.core.managers import TicketManager
from teatree.core.modelkit.gate_registry import get_gate
from teatree.core.modelkit.review_state import ReviewState
from teatree.core.models.auto_implement import is_auto_implement
from teatree.core.models.ticket_evidence import TicketEvidenceModel
from teatree.core.models.ticket_introspection import TicketIntrospectionModel
from teatree.core.models.ticket_ledger import retire_phase_ledger
from teatree.core.models.ticket_number import derive_issue_number
from teatree.core.models.ticket_overlay import TicketOverlayModel
from teatree.core.models.ticket_phase_sessions import TicketPhaseSessionModel
from teatree.core.models.ticket_scheduling import TicketSchedulingModel
from teatree.core.models.ticket_state_sets import TicketStateSetsModel
from teatree.core.models.ticket_status import TicketStatusModel
from teatree.utils.url_slug import repo_namespaced_key as compute_repo_namespaced_key

logger = logging.getLogger(__name__)


def _check_plan_artifact(ticket: object) -> bool:
    return bool(get_gate("plan_artifact")(ticket))


if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.types import TicketExtra, TicketSiblingFields


# Composed via Django abstract-model facets (the framework's own model-decomposition
# pattern) rather than composed attributes: the facets carry cohesive instance
# behaviour while every method stays reachable as ``ticket.foo()``, so the large
# consumer-facing API and the FSM state graph are preserved with zero call-site
# churn. The concrete class owns the fields, the state graph, and ``save``.
# ``models.Model`` is not re-listed as a base: every facet already derives from it
# via ``TicketFacet``, so it is redundant here.
class Ticket(
    TicketOverlayModel,
    TicketPhaseSessionModel,
    TicketSchedulingModel,
    TicketEvidenceModel,
    TicketStatusModel,
    TicketStateSetsModel,
    TicketIntrospectionModel,
):
    class State(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SCOPED = "scoped", "Scoped"
        # Work begins BEFORE the plan is recorded — `plan()` is the
        # STARTED -> PLANNED transition and it requires a PlanArtifact. Labelled
        # "Started"/"Planned" the pair reads as though planning should come
        # first; naming the milestone rather than the status makes the real
        # order self-evident on the board.
        STARTED = "started", "Work started"
        PLANNED = "planned", "Plan recorded"
        CODED = "coded", "Coded"
        TESTED = "tested", "Tested"
        # "Self-reviewed", not "Reviewed": this is the author's own pre-ship pass,
        # which the FSM places BEFORE shipping. Peer review is IN_REVIEW, two
        # columns later. Labelled "Reviewed" the two read as one phase in the
        # wrong order, and the board looks mis-sequenced when it matches the FSM.
        REVIEWED = "reviewed", "Self-reviewed"
        SHIPPED = "shipped", "Shipped"
        IN_REVIEW = "in_review", "In peer review"
        MERGED = "merged", "Merged"
        RETROSPECTED = "retrospected", "Retrospected"
        DELIVERED = "delivered", "Delivered"
        # Reviewer terminal — a posted external review is done, NOT author-merged
        # (DELIVERED); keeps a reviewer ticket off the board's "Landed" group.
        REVIEW_POSTED = "review_posted", "Review posted"
        IGNORED = "ignored", "Ignored"

    class Role(models.TextChoices):
        AUTHOR = "author", "Author"
        REVIEWER = "reviewer", "Reviewer"

    class Kind(models.TextChoices):
        FEATURE = "feature", "Feature"
        FIX = "fix", "Fix"

    # #808: the ship reconcile is PHASE-DRIVEN / state-complete, not an
    # enumerated source allow-list. The shipping gate already verified the
    # aggregated cross-session phase ledger (the single source of truth)
    # before calling ``reconcile_reviewed``; the FSM must follow the
    # phases, not gate them behind a hand-maintained state list (the
    # recurring #798/#799/#808 ``{'allowed': False, 'missing': []}``
    # class — each new unlisted non-terminal state re-broke it). Only
    # genuinely terminal/abandoned states are non-recoverable; EVERY other
    # state is a legal reconcile source, derived (not enumerated) so a
    # future added state cannot silently re-introduce the bug.
    _TERMINAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {State.SHIPPED, State.MERGED, State.DELIVERED, State.REVIEW_POSTED, State.IGNORED},
    )
    # The linear author work-state progression (excludes the terminal set and the
    # off-ladder IN_REVIEW/RETROSPECTED branch states). A ticket at index i has
    # produced every phase output up to and including index i — the ordering
    # ``has_completed_phase`` reads to tell a live phase apart from a superseded one.
    _WORK_STATE_ORDER: ClassVar[tuple[str, ...]] = (
        State.NOT_STARTED,
        State.SCOPED,
        State.STARTED,
        State.PLANNED,
        State.CODED,
        State.TESTED,
        State.REVIEWED,
        State.SHIPPED,
    )
    # The work-state each author phase PRODUCES on success. A FAILED task whose
    # phase output the ticket's FSM already reached is SUPERSEDED — an earlier
    # interrupted run left the dead row while the ticket advanced on its own — so
    # re-dispatching or escalating that task only floods the away-mode queue.
    _PHASE_PRODUCES_STATE: ClassVar[dict[str, str]] = {
        "planning": State.PLANNED,
        "coding": State.CODED,
        "testing": State.TESTED,
        "reviewing": State.REVIEWED,
        "shipping": State.SHIPPED,
    }
    # NOTE: a class-body comprehension cannot see the enclosing ``State``
    # (Python scoping); enumerate explicitly and assert completeness in a
    # test so a future added state is caught rather than silently dropped.
    _RECONCILE_SOURCE_STATES: ClassVar[list[str]] = [
        State.NOT_STARTED,
        State.SCOPED,
        State.STARTED,
        State.PLANNED,
        State.CODED,
        State.TESTED,
        State.REVIEWED,
        State.IN_REVIEW,
        State.RETROSPECTED,
    ]
    # #1343: PR-merge reconcile catches every PRE-MERGED state. The
    # original guard only fired ``mark_merged()`` from IN_REVIEW/MERGED,
    # so tickets whose PR landed while the FSM still read STARTED stayed
    # stuck on the statusline. The merge keystone calls
    # ``reconcile_merged()``, which targets MERGED from every pre-merged
    # state (and is idempotent at MERGED). RETROSPECTED/DELIVERED are
    # past MERGED and must not be dragged backward; IGNORED is abandoned.
    _MERGED_RECONCILE_SOURCE_STATES: ClassVar[list[str]] = [
        State.NOT_STARTED,
        State.SCOPED,
        State.STARTED,
        State.PLANNED,
        State.CODED,
        State.TESTED,
        State.REVIEWED,
        State.SHIPPED,
        State.IN_REVIEW,
        State.MERGED,
    ]

    overlay = models.CharField(max_length=255)
    issue_url = models.URLField(max_length=500, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    repos = models.JSONField(default=list, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.AUTHOR)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.FEATURE)
    extra = models.JSONField(default=dict, blank=True)
    context = models.TextField(blank=True, default="")
    short_description = models.CharField(max_length=80, blank=True, default="")
    # Set to True when the remote forge returns HTTP 404; the disposition scanner
    # then excludes this ticket from future fetches (#1875).
    remote_missing = models.BooleanField(default=False)
    # Expedite / release-blocker flag (PR-07): the flag alone grants NO merge
    # bypass. It makes a per-CLEAR, human-authorized, SHA-bound PENDING-checks
    # waiver ISSUABLE (see ``MergeClear.issue`` / ``expedite_pending_waived_by``):
    # a flagged ticket's merge can proceed on queued (pending) required checks
    # ONLY with a recorded human authoriser and a tree-bound local-CI-green
    # attestation. A FAILED required check is never waivable. Surfaces on the
    # ticket CLI and a statusline chip.
    expedited = models.BooleanField(default=False)
    # Collision-free ``<repo-slug>#<issue-number>`` derived from `issue_url`
    # (#2293): a bare numeric IID may collide across repos, this key never
    # does. Blank when `issue_url` is a PR/MR reference, a bare number, or
    # any other non-issue shape — see `repo_namespaced_key_from_path`.
    repo_namespaced_key = models.CharField(max_length=300, blank=True, default="", db_index=True)
    # Denormalized forge issue number (trailing digits of `issue_url`, blank when
    # there is none), kept in sync by ``save`` — the indexed backing that turns
    # ``_ticket_by_number`` from an O(all tickets) Python scan into an O(1)
    # lookup. The ``ticket_number`` property composes it with the pk fallback.
    issue_number = models.CharField(max_length=32, blank=True, default="", db_index=True)

    objects = TicketManager()

    class Meta:
        db_table = "teatree_ticket"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["issue_url"],
                name="unique_nonempty_issue_url",
                condition=~models.Q(issue_url=""),
            ),
            models.UniqueConstraint(
                fields=["repo_namespaced_key"],
                name="unique_nonempty_repo_namespaced_key",
                condition=~models.Q(repo_namespaced_key=""),
            ),
        ]

    def __str__(self) -> str:
        return str(self.issue_url or f"ticket-{self.pk}")

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.overlay and self.issue_url:
            self.overlay = self._infer_overlay()
        if not self.repo_namespaced_key and self.issue_url:
            self.repo_namespaced_key = compute_repo_namespaced_key(self.issue_url)
        self.issue_number = derive_issue_number(self.issue_url)
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def stamp_issue_title(self, title: str) -> list[str]:
        """Persist the forge issue *title* onto this ticket for the dashboard."""
        if not title:
            return []
        extra = self.extra if isinstance(self.extra, dict) else {}
        set_keys: TicketExtra = {}
        also_set: TicketSiblingFields = {}
        written: list[str] = []
        if not extra.get("issue_title"):
            set_keys["issue_title"] = title
            written.append("extra")
        if seed := self.short_description_seed(title):
            also_set["short_description"] = seed
            written.append("short_description")
        if written:
            self.merge_extra(set_keys=set_keys or None, also_set=also_set or None)
        return written

    def short_description_seed(self, title: str) -> str:
        """The card label a blank ``short_description`` takes from *title*."""
        if not title or self.short_description:
            return ""
        return title[: self._meta.get_field("short_description").max_length or 80]

    @transition(field=state, source=State.NOT_STARTED, target=State.SCOPED)
    def scope(
        self,
        *,
        issue_url: str | None = None,
        variant: str | None = None,
        repos: list[str] | None = None,
    ) -> None:
        if issue_url is not None:
            self.issue_url = issue_url
        if variant is not None:
            self.variant = variant
        if repos is not None:
            self.repos = repos

    @transition(field=state, source=[State.SCOPED, State.STARTED], target=State.STARTED)
    def start(self) -> None:
        """Schedule worktree provisioning + planning task."""

    @transition(
        field=state,
        source=State.STARTED,
        target=State.PLANNED,
        conditions=[_check_plan_artifact],
    )
    def plan(self, *, parent_task: "Task | None" = None) -> None:
        """Advance STARTED → PLANNED after a PlanArtifact record exists."""
        self._consume_pending_phase_tasks("planning")
        self.schedule_coding(parent_task=parent_task)

    @transition(field=state, source=State.PLANNED, target=State.CODED)
    def code(self, *, parent_task: "Task | None" = None) -> None:
        get_gate("plan_currency")(self)  # SELFCATCH-3: refuse a thin/stale plan (NO-OP unless flag on).
        self._refuse_if_worktree_dirty("coding")
        self._consume_pending_phase_tasks("coding")
        self.schedule_testing(parent_task=parent_task)

    @transition(
        field=state,
        source=[State.NOT_STARTED, State.SCOPED, State.STARTED],
        target=State.CODED,
        conditions=[is_auto_implement],
    )
    def code_direct(self, *, parent_task: "Task | None" = None) -> None:
        """Advance a plan-skipped auto-implement ticket straight to CODED."""
        self._refuse_if_worktree_dirty("coding")
        self._consume_pending_phase_tasks("coding")
        self.schedule_testing(parent_task=parent_task)

    @transition(field=state, source=State.CODED, target=State.TESTED)
    def test(self, *, passed: bool = True, parent_task: "Task | None" = None) -> None:
        self._refuse_if_worktree_dirty("testing")
        extra = self._extra()
        extra["tests_passed"] = passed
        self.extra = extra
        self._consume_pending_phase_tasks("testing")
        self.schedule_review(parent_task=parent_task)

    @transition(
        field=state,
        source=State.TESTED,
        target=State.REVIEWED,
        conditions=[
            lambda t: t.tasks.completed_in_phase("reviewing").exists(),
            lambda t: t.review_context_satisfied(),
        ],
    )
    def review(self, *, parent_task: "Task | None" = None) -> None:
        self._refuse_if_worktree_dirty("reviewing")
        self._consume_pending_phase_tasks("reviewing")
        if self.has_shippable_diff():
            self.schedule_shipping(parent_task=parent_task)
            return
        logger.info(
            "Ticket %s reviewed with no shippable diff; skipping auto-shipping (likely meta or already-shipped work)",
            self.pk,
        )
        extra = self._extra()
        extra["shipping_skipped"] = "no shippable diff — likely meta or already-shipped work"
        self.extra = extra

    @transition(
        field=state,
        source=_RECONCILE_SOURCE_STATES,
        target=State.REVIEWED,
    )
    def reconcile_reviewed(self) -> None:
        """Phase-driven, state-complete FSM catch-up to REVIEWED (#694, #798, #799, #808)."""

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
            # A re-review on a NEW head SHA (``ReviewedPrHeadScanner`` →
            # ``reviewer_pr.new_sha``) schedules its task on a ticket that is
            # already REVIEW_POSTED from the previous pass. Without this
            # self-transition ``Task.complete()``'s derived-source guard skips
            # the FSM advance and ``last_review_state`` is never re-stamped, so
            # the reviewed-at record stays half-written and the ticket drops out
            # of the re-review watch set after the first push. Same shape and
            # same rationale as #1431's self-transition on the sibling
            # ``mark_review_no_action`` below; SHIPPED/MERGED/IGNORED stay out
            # for the same reason (an IGNORED→REVIEW_POSTED move would resurrect).
            State.REVIEW_POSTED,
        ],
        target=State.REVIEW_POSTED,
        conditions=[
            lambda t: t.role == Ticket.Role.REVIEWER and t.tasks.completed_in_phase("reviewing").exists(),
            lambda t: t.review_context_satisfied(),
        ],
    )
    def mark_reviewed_externally(self) -> None:
        """Reviewer-role short-circuit: any pre-shipped state → REVIEW_POSTED."""
        sha = str(self._extra().get("reviewed_sha", ""))
        if self.issue_url and sha:
            # #800 N3: canonical locked RMW — a concurrent pr_urls /
            # visual_qa writer no longer clobbers reviewed_sha /
            # last_review_state.
            self.merge_extra(set_keys={"reviewed_sha": sha, "last_review_state": ReviewState.APPROVED.value})

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
            # #1431: REVIEW_POSTED self-transition (this transition's own target)
            # makes a re-dispatched orphan's no-action path a no-op instead of
            # a TransitionNotAllowed crash. SHIPPED/MERGED/IGNORED stay out —
            # an IGNORED→REVIEW_POSTED move would resurrect; Gap B reaps those.
            State.REVIEW_POSTED,
        ],
        target=State.REVIEW_POSTED,
        conditions=[lambda t: t.role == Ticket.Role.REVIEWER],
    )
    def mark_review_no_action(self) -> None:
        """Reviewer-role terminal disposition for a no-postable-action review."""
        sha = str(self._extra().get("reviewed_sha", ""))
        if self.issue_url and sha:
            self.merge_extra(set_keys={"reviewed_sha": sha, "last_review_state": ReviewState.REVIEWED_NO_ACTION.value})
        self._consume_pending_phase_tasks("reviewing")

    @transition(field=state, source=[State.REVIEWED, State.SHIPPED], target=State.SHIPPED)
    def ship(self) -> None:
        """Schedule push + PR creation."""
        self._refuse_if_worktree_dirty("shipping")
        get_gate("local_e2e_dod")(self)
        get_gate("forced_repro")(self)
        self._consume_pending_phase_tasks("shipping")

    @transition(field=state, source=State.SHIPPED, target=State.IN_REVIEW)
    def request_review(self) -> None:
        pass

    @transition(field=state, source=[State.IN_REVIEW, State.MERGED], target=State.MERGED)
    def mark_merged(self) -> None:
        """Schedule worktree teardown."""
        get_gate("merge_evidence")(self)

    @transition(
        field=state,
        source=_MERGED_RECONCILE_SOURCE_STATES,
        target=State.MERGED,
    )
    def reconcile_merged(self) -> None:
        """State-complete FSM catch-up to ``MERGED`` on PR-merge (#1343)."""
        get_gate("merge_evidence")(self)

    @transition(field=state, source=[State.MERGED, State.RETROSPECTED], target=State.RETROSPECTED)
    def retrospect(self) -> None:
        """Schedule retrospection I/O."""

    @transition(field=state, source=State.RETROSPECTED, target=State.DELIVERED)
    def mark_delivered(self) -> None:
        """Reach DELIVERED past the Definition-of-Done gates — each NO-OP unless configured."""
        get_gate("fix_record_dod")(self)
        get_gate("spec_coverage")(self)
        get_gate("integration_review")(self)
        get_gate("critic")(self)

    @transition(field=state, source=[State.MERGED, State.DELIVERED], target=State.REVIEWED)
    def reopen_for_followup(self) -> None:
        """Reopen a terminally-shipped ticket to REVIEWED for a follow-up PR (#3327)."""

    @transition(field=state, source=[State.CODED, State.TESTED, State.REVIEWED], target=State.STARTED)
    def rework(self) -> None:
        extra = self._extra()
        extra.pop("tests_passed", None)
        self.extra = extra
        self._cancel_pending_tasks()

    @transition(
        field=state,
        source=[State.SHIPPED, State.IN_REVIEW, State.MERGED, State.RETROSPECTED, State.DELIVERED],
        target=State.STARTED,
    )
    def reopen(self) -> None:
        """Reopen a post-ship ticket back to STARTED.

        DELIVERED is a source because it is otherwise the end of every path: a ticket
        owns its issue URL in each state but IGNORED, so a reopened issue behind a
        delivered ticket was invisible to intake and to every reconcile rule (#4152).
        REVIEW_POSTED stays out — a reviewer ticket's ``issue_url`` IS a PR, which the
        board reconcile's PR rules already resolve.
        """
        extra = self._extra()
        extra.pop("tests_passed", None)
        extra["reopened_from"] = self.state
        self.extra = extra
        self._cancel_pending_tasks()
        retire_phase_ledger(self)

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.PLANNED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
            State.SHIPPED,
            State.IN_REVIEW,
            State.MERGED,
            State.RETROSPECTED,
        ],
        target=State.IGNORED,
    )
    def ignore(self) -> None:
        extra = self._extra()
        extra["ignored_from"] = self.state
        self.extra = extra
