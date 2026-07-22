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
    TicketIntrospectionModel,
):
    class State(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SCOPED = "scoped", "Scoped"
        STARTED = "started", "Started"
        PLANNED = "planned", "Planned"
        CODED = "coded", "Coded"
        TESTED = "tested", "Tested"
        REVIEWED = "reviewed", "Reviewed"
        SHIPPED = "shipped", "Shipped"
        IN_REVIEW = "in_review", "In review"
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

    @classmethod
    def marker_release_states(cls) -> frozenset[str]:
        """Terminal-done states that free markers and trigger worktree teardown.

        ``_TERMINAL_STATES`` minus SHIPPED (its PR is still open). Shared by the
        teardown/marker signal and the #3275 reconciler; REVIEW_POSTED is the
        reviewer terminal (marker release is a no-op for reviewer tickets).
        """
        return frozenset({cls.State.MERGED, cls.State.DELIVERED, cls.State.REVIEW_POSTED, cls.State.IGNORED})

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
        """Persist the forge issue *title* onto this ticket for the dashboard.

        Stores the full title under ``extra['issue_title']`` (the input the
        summariser reads) and seeds ``short_description`` with the title,
        truncated to the column width, so a card shows a human label
        immediately — before any LLM-refined summary. Never clobbers an
        existing value, and a blank title is a no-op. Returns the fields
        written (empty when nothing changed), saving only those via the locked
        ``merge_extra`` primitive.
        """
        if not title:
            return []
        extra = self.extra if isinstance(self.extra, dict) else {}
        set_keys: TicketExtra = {}
        also_set: TicketSiblingFields = {}
        written: list[str] = []
        if not extra.get("issue_title"):
            set_keys["issue_title"] = title
            written.append("extra")
        if not self.short_description:
            max_len = self._meta.get_field("short_description").max_length or 80
            also_set["short_description"] = title[:max_len]
            written.append("short_description")
        if written:
            self.merge_extra(set_keys=set_keys or None, also_set=also_set or None)
        return written

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
        """Schedule worktree provisioning + planning task.

        The worker creates per-repo git worktrees, then calls
        ``schedule_planning()`` once the layout exists. FSM invariant (BLUEPRINT
        §4): transition bodies stay pure — long I/O is offloaded to an
        ``@task`` worker, enqueued after commit so the state change and the
        queued work land atomically.

        Source ``[SCOPED, STARTED]`` makes re-firing idempotent: if the previous
        provisioning worker failed, the operator can re-call ``start()``
        without rolling back through ``rework``. The worker's own state guard
        prevents duplicate work when provisioning already succeeded.

        The ``execute_provision`` enqueue is the ``post_transition`` receiver's
        job (``teatree.core.signals``), keyed on the transition name — the body
        stays free of the task up-edge (#2385).
        """

    @transition(
        field=state,
        source=State.STARTED,
        target=State.PLANNED,
        conditions=[_check_plan_artifact],
    )
    def plan(self, *, parent_task: "Task | None" = None) -> None:
        """Advance STARTED → PLANNED after a PlanArtifact record exists.

        Guarded by check_plan_artifact() — requires at least one PlanArtifact
        row for this ticket.  The condition is the single source of truth for
        the plan gate; no prose rule or wall-clock check is needed.
        """
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
        """Advance a plan-skipped auto-implement ticket straight to CODED.

        The issue-implementer auto-start path
        (``persistence._handle_orchestrator``) schedules a ``coding`` task
        directly, skipping the scope/plan phases, so the normal ``code()`` guard
        (``source=PLANNED``) can never fire when that task completes — the wedge
        that left tickets with completed coding yet zero transitions and no PR.
        This is the plan-skipped sibling of ``code()``, reachable ONLY for a
        ticket carrying the ``auto_implement`` marker (the ``is_auto_implement``
        condition), so the normal author flow's plan gate is untouched. Unlike
        ``code()`` it runs no ``plan_currency`` gate (there is no plan to check),
        but it keeps the same dirty-worktree preflight and schedules testing, so
        the FSM proceeds coding -> testing -> reviewing -> shipping instead of
        silently no-opping.
        """
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
        """Phase-driven, state-complete FSM catch-up to REVIEWED (#694, #798, #799, #808).

        EVERY non-terminal state reconciles to ``REVIEWED`` — the source
        set is *derived* from ``_RECONCILE_SOURCE_STATES`` (all states
        except the terminal ``_TERMINAL_STATES``: SHIPPED/MERGED/DELIVERED/
        IGNORED), never a hand-maintained allow-list.

        The shipping gate is the single source of truth: it verifies the
        required phases aggregated across **all** of the ticket's sessions
        (``aggregate_phase_records``/``check_gate_across_ticket``) *before*
        calling this, so a passing gate implies a shippable FSM state and
        ``ship()`` never raises a raw ``TransitionNotAllowed`` at ``pr create``.
        Deriving the source from the terminal set (not a hand-kept list) means a
        newly added non-terminal state cannot silently re-break the gate.
        Terminal states stay non-recoverable: SHIPPED/MERGED/DELIVERED are
        post-ship success and IGNORED is abandoned — none reconcile backward.

        This transition body stays pure: task ledger consumption is the
        caller's responsibility on the gate-verified path
        (``reconcile_fsm_for_ship``). Calling this directly from the
        ungated ``ticket transition`` CLI or from ``--skip-validation``
        must NOT complete active reviewing tasks — those paths skip the
        attestation that would justify it.
        """

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
        ],
        target=State.REVIEW_POSTED,
        conditions=[
            lambda t: t.role == Ticket.Role.REVIEWER and t.tasks.completed_in_phase("reviewing").exists(),
            lambda t: t.review_context_satisfied(),
        ],
    )
    def mark_reviewed_externally(self) -> None:
        """Reviewer-role short-circuit: any pre-shipped state → REVIEW_POSTED.

        External review tickets bypass the implementation lifecycle. Once the
        reviewing task completes the ticket is done. Lands ``REVIEW_POSTED``,
        NOT ``DELIVERED`` (author work merged to main), so the board never shows
        a reviewer ghost as "Landed". Also stamps the head SHA +
        ``last_review_state`` on ``extra`` so ``ReviewerPrsScanner`` won't
        re-spawn the reviewer agent until the SHA moves or the approval drops.
        """
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
        """Reviewer-role terminal disposition for a no-postable-action review.

        Sibling of :meth:`mark_reviewed_externally` for the case the reviewer
        concludes an external review with nothing to post or approve (e.g. a
        bot MR — Aikido/Dependabot — no diff worth commenting on, no approval
        to give). Without it the reviewing Task never reaches a terminal state
        (the only other path, ``Task.complete()`` → ``mark_reviewed_externally``,
        requires APPROVED), so ``pending-spawn`` re-dispatched it forever
        (#1077).

        Unlike ``mark_reviewed_externally`` (fired *from* an
        already-COMPLETED task) this transition is driven directly via
        ``t3 teatree ticket transition <id> mark_review_no_action`` while the
        reviewing task is still PENDING, so it consumes that task itself.
        It records ``last_review_state = REVIEWED_NO_ACTION`` (NEVER APPROVED):
        the dedup's APPROVED-only suppression therefore does not hide a future
        *genuine* review, while ``_already_reviewed_at_head``
        still treats a no-action observation at the current head SHA as
        "already handled" so the task is not re-queued. A head-SHA move drops
        ``last_review_state`` (the existing #959 reset) so a new revision is
        still reviewed — no lost obligation.
        """
        sha = str(self._extra().get("reviewed_sha", ""))
        if self.issue_url and sha:
            self.merge_extra(set_keys={"reviewed_sha": sha, "last_review_state": ReviewState.REVIEWED_NO_ACTION.value})
        self._consume_pending_phase_tasks("reviewing")

    @transition(field=state, source=[State.REVIEWED, State.SHIPPED], target=State.SHIPPED)
    def ship(self) -> None:
        """Schedule push + PR creation.

        The worker pushes the worktree branch, opens the pull request, and
        calls ``request_review()`` on success. FSM invariant (BLUEPRINT §4):
        transition bodies stay pure — long I/O is offloaded to an ``@task``
        worker, enqueued after commit so the state change and the queued work
        land atomically.

        Source ``[REVIEWED, SHIPPED]`` makes re-firing idempotent: if the
        previous ship worker failed (push rejected, code host unavailable,
        credentials missing), the operator can re-call ``ship()`` to retry.
        The worker's own state guard skips duplicate work if push already
        succeeded.

        Three preflight guards run first, each raising an
        :class:`InvalidTransitionError` subclass (the outer atomic rolls back):
        ``_refuse_if_worktree_dirty`` (#884), the #88 DoD gate
        (``check_local_e2e_dod``, green local E2E for a UI-visible ticket), and
        the #118 forced-repro gate (``check_forced_repro``, a no-op while off).
        """
        self._refuse_if_worktree_dirty("shipping")
        get_gate("local_e2e_dod")(self)
        get_gate("forced_repro")(self)
        self._consume_pending_phase_tasks("shipping")

    @transition(field=state, source=State.SHIPPED, target=State.IN_REVIEW)
    def request_review(self) -> None:
        pass

    @transition(field=state, source=[State.IN_REVIEW, State.MERGED], target=State.MERGED)
    def mark_merged(self) -> None:
        """Schedule worktree teardown.

        The worker removes git worktrees, deletes the local branch, drops the
        per-worktree DB and runs overlay cleanup hooks. FSM invariant (BLUEPRINT
        §4): transition bodies stay pure — long I/O is offloaded to an ``@task``
        worker, enqueued after commit so state change and queued work land atomically.

        Source ``[IN_REVIEW, MERGED]`` makes re-firing idempotent: if a previous
        teardown reported errors, the operator can re-call ``mark_merged()`` to
        retry. The worker is best-effort and does not advance the FSM, so retries
        are safe. The ``execute_teardown`` enqueue is the ``post_transition``
        receiver's job (``teatree.core.signals``), keyed on the transition (#2385).

        The ``merge_evidence`` gate (#4a) preflights: MERGED is unreachable without
        a real merged-SHA row, so the ungated ``_advance_ticket`` walk fails loud.
        """
        get_gate("merge_evidence")(self)

    @transition(
        field=state,
        source=_MERGED_RECONCILE_SOURCE_STATES,
        target=State.MERGED,
    )
    def reconcile_merged(self) -> None:
        """State-complete FSM catch-up to ``MERGED`` on PR-merge (#1343).

        The merge keystone (``merge.execution.record_merge_and_advance``) calls
        this from its post hook: an authorised, audited PR-merge is the authority
        — whatever pre-merged state the ticket sat in, the FSM must follow. Mirrors
        ``reconcile_reviewed`` (#808) — the source is derived from the pre-merged
        set so a future-added pre-merged state cannot re-introduce the stale class.
        Post-MERGED states (``RETROSPECTED``/``DELIVERED``) and ``IGNORED`` are NOT
        sources: the keystone must never drag a ticket BACKWARD past MERGED.

        Schedules the same teardown work as ``mark_merged`` (the ``post_transition``
        receiver enqueues ``execute_teardown`` for this transition too, #2385).

        Gated by ``merge_evidence`` (#4a): the keystone writes the MergeAudit row
        BEFORE this call, so authorised merges pass and an evidence-less reconcile is refused.
        """
        get_gate("merge_evidence")(self)

    @transition(field=state, source=[State.MERGED, State.RETROSPECTED], target=State.RETROSPECTED)
    def retrospect(self) -> None:
        """Schedule retrospection I/O.

        The worker writes retro artifacts and calls ``mark_delivered()`` on
        success. FSM invariant (BLUEPRINT §4): transition bodies stay pure —
        long I/O is offloaded to an ``@task`` worker, enqueued after commit so
        the state change and the queued work land atomically.

        Source ``[MERGED, RETROSPECTED]`` makes re-firing idempotent: if a
        previous retro worker failed, the operator can re-call ``retrospect()``
        to retry. The worker's own state guard skips when retrospection
        already produced its artifacts.

        The ``execute_retrospect`` enqueue is the ``post_transition`` receiver's
        job (``teatree.core.signals``), keyed on the transition name (#2385).
        """

    @transition(field=state, source=State.RETROSPECTED, target=State.DELIVERED)
    def mark_delivered(self) -> None:
        """Reach DELIVERED past the Definition-of-Done gates — each NO-OP unless configured.

        ``fix_record_dod``, ``spec_coverage`` (#2232), ``integration_review`` (PR-08), and
        ``critic`` (SELFCATCH-5 — a CriticFinding per failing class, advisory) gate the close.
        """
        get_gate("fix_record_dod")(self)
        get_gate("spec_coverage")(self)
        get_gate("integration_review")(self)
        get_gate("critic")(self)

    @transition(field=state, source=[State.MERGED, State.DELIVERED], target=State.REVIEWED)
    def reopen_for_followup(self) -> None:
        """Reopen a terminally-shipped ticket to REVIEWED for a follow-up PR (#3327).

        One ticket → N PRs: the narrow "new branch on the same ticket" edge that
        ``pr create --adopt-worktree`` fires so PR-B can ship after PR-A merged.
        Only MERGED/DELIVERED need it (SHIPPED ships directly; IN_REVIEW/
        RETROSPECTED reconcile to REVIEWED; IGNORED is abandoned). Pure body that
        keeps the phase ledger (unlike ``reopen()``): the follow-up re-ships from
        REVIEWED, then gets its own review via SHIPPED → IN_REVIEW. Re-shipping
        merged work is foreclosed upstream by the #788 hollow-ship guard.
        """

    @transition(field=state, source=[State.CODED, State.TESTED, State.REVIEWED], target=State.STARTED)
    def rework(self) -> None:
        extra = self._extra()
        extra.pop("tests_passed", None)
        self.extra = extra
        self._cancel_pending_tasks()

    @transition(
        field=state,
        source=[State.SHIPPED, State.IN_REVIEW, State.MERGED, State.RETROSPECTED],
        target=State.STARTED,
    )
    def reopen(self) -> None:
        """Reopen a post-ship ticket back to STARTED.

        Triggered when new draft MRs appear after the ticket was shipped,
        indicating additional work is needed.

        #1286: retire every session's phase ledger here — ``reopen()`` is
        the explicit workstream-boundary transition. Without this, the
        prior workstream's ``testing``/``reviewing`` attestations remain
        in ``aggregate_phase_records()`` and false-pass the next
        workstream's shipping gate (the ``AGENTS.md`` § "Reused-ticket
        attestation" risk). Same operation the sanctioned
        ``lifecycle clear-ledger --confirm`` performs, run inside the FSM
        transition body so the cross-workstream gate-bypass is structurally
        foreclosed rather than relying on the agent remembering to call
        ``clear-ledger`` on reuse.
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
