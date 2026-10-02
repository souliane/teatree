import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from teatree.core.provision.provision_report import ProvisionReportDict

logger = logging.getLogger(__name__)

type Ports = dict[str, int]

# An arbitrary JSON object stored under a ``extra`` key (heterogeneous, open
# shape — e.g. ``pr_url_by_branch``), so a fixed TypedDict does not fit.
type JSONObject = dict[str, object]


class SlackAnswerContext(TypedDict, total=False):
    """The reactive Slack-answer cycle's stamp on a conversation lane's ticket (#4527).

    ``implies_work`` decides whether the answering phase owes a ``work_item``, and
    ``work_issue_url`` records the findable row the request became — the two keys that
    separate a handled request from a dropped one.
    """

    channel: str
    slack_ts: str
    coalesced_ts: list[str]
    question: str
    fingerprint: str
    intent: str
    implies_work: bool
    work_summary: str
    work_issue_url: str


class VisualQAPageError(TypedDict):
    kind: str
    message: str


class VisualQAPageDetail(TypedDict):
    url: str
    errors: list[VisualQAPageError]


class VisualQASummary(TypedDict, total=False):
    targets: list[str]
    skipped_reason: str
    base_url: str
    pages_checked: int
    errors: int
    details: list[VisualQAPageDetail]


class PRApprovalsSerialized(TypedDict, total=False):
    """The approval tally denormalized onto a ``PREntrySerialized`` (F1.7).

    ``count`` is how many approvals the PR currently carries; ``required`` is
    the threshold the forge enforces (defaults to 1 when absent). Read by the
    dashboard ``_check_pr`` selector to raise a ``needs_approval`` action item
    while ``count < required``.
    """

    count: int
    required: int


class PRDiscussionSerialized(TypedDict, total=False):
    """One denormalized review-thread entry on a ``PREntrySerialized`` (F1.7).

    ``status`` is the thread's resolution state (``waiting_reviewer`` /
    ``needs_reply`` / ``addressed``); ``detail`` is the short human-readable
    summary the dashboard surfaces. Structurally mirrors the read-model
    ``selectors._types.DiscussionData`` but is declared here so ``core.models``
    keeps no backwards edge onto ``core.selectors``.
    """

    status: str
    detail: str


class PREntrySerialized(TypedDict, total=False):
    url: str
    title: str
    branch: str
    draft: bool
    repo: str
    iid: int
    pipeline_status: str
    pipeline_url: str
    review_requested: bool
    reviewer_names: list[str]
    head_sha: str
    last_reviewed_sha: str
    # Denormalized action-required inputs read by the dashboard ``_check_pr``
    # selector (F1.7): the forge lifecycle state, the review Slack permalink,
    # the approval tally, the review-thread entries, and the pending-draft
    # review-comment signals.
    state: str
    review_permalink: str
    approvals: PRApprovalsSerialized
    discussions: list[PRDiscussionSerialized]
    draft_comments_pending: bool
    draft_comments_count: int


class TicketExtra(TypedDict, total=False):
    tests_passed: bool
    # Self-improvement repair tickets need this marker to classify their own
    # task failures separately from the incidents they are investigating.
    source: str
    pr_urls: list[str]
    # #1263: per-branch PR URL index so a reused-ticket multi-workstream
    # ship can tell whether the *current* invoking branch's PR exists,
    # without short-circuiting on the truthiness of the shared ``pr_urls``.
    pr_url_by_branch: dict[str, str]
    # ARBITER (F1.3): the ``PullRequest`` rows are the system of record for PR
    # facts (their FSM ``state`` is the authority). This ``prs`` map is a
    # DENORMALIZED SYNC CACHE — a JSON snapshot the dashboard read-model
    # (``selectors.automation._check_pr``) consumes so it need not re-query the
    # forge. When the two disagree, the ``PullRequest`` row wins; a stale entry
    # here is a cache-refresh gap, never a source of truth. Read-path
    # unification onto the rows is deferred.
    prs: dict[str, PREntrySerialized]
    pr_title_override: str
    ship_invoking_branch: str
    ship_invoking_path: str
    ignored_from: str
    reopened_from: str
    # Board reconcile rule E's revival cap. Undeclared it is stripped by every
    # ladder transition, so the cap reads 0 forever and never fires (#4152).
    reopen_revivals: int
    # Board reconcile rule F's record of WHY the forge closed a retired pre-ship
    # ticket's issue. Undeclared it is stripped by ``ignore()``'s own rewrite, leaving
    # a NOT_PLANNED retirement indistinguishable from a COMPLETED one (#4711).
    issue_close_reason: str
    visual_qa: VisualQASummary
    branch: str
    # #33 per-repo branch override map (repo → branch). A ticket whose repos
    # live on DIFFERENT branches maps each one here; ``branch`` stays the
    # single ticket-dir name so every repo provisions as a SIBLING in one dir.
    # Repos absent from the map fall back to ``branch``.
    branches: dict[str, str]
    # #2275 adopt: repo -> existing on-disk worktree_path for an outside branch
    # registered via ``workspace ticket --adopt``; the provisioner records the
    # path verbatim instead of ``git worktree add``.
    adopt: dict[str, str]
    description: str
    provision: dict[str, str]
    shipping_skipped: str
    tracker_status: str
    # Notion status-sync: the tracked page URL (input) and the last status read
    # from it via the direct Notion API (``core.sync.fetch_notion_statuses``).
    notion_url: str
    notion_status: str
    issue_title: str
    labels: list[str]
    reviewed_sha: str
    # What the FORGE last reported. Overwritten with the live value on every scan, so a
    # LOCAL disposition stored here survives one pass and is then read back as an
    # observation — the discharge has its own key below for exactly that reason.
    last_review_state: str
    # The SHA the factory's own review was discharged at, written by the discharge
    # transitions. Durable: nothing that caches forge state may overwrite it.
    discharged_sha: str
    # ``ReviewedPrHeadScanner``'s rotation clock — least-recently-checked first,
    # so its per-tick cap cannot pin one window of a longer watch list.
    head_checked_at: str
    retro_scheduled: bool
    tracker_404: bool
    more_prs_coming: bool
    e2e_recipe: "E2ERecipeSerialized"
    # #940 branch-currency gate state: post-merge SHA the cold reviewer
    # must attest, and a durable refusal entry when the ship-time
    # defense-in-depth re-check rejects the push.
    # Stacked-delivery override keyed by canonical owner/repo slug. The string
    # shape is retained only for tickets written before repo scoping.
    target_branch: dict[str, str] | str
    branch_currency_post_merge_sha: str
    ship_branch_currency_blocker: "BranchCurrencyBlocker"
    last_approval_sha: str
    # #88 DoD gate escape hatch: an explicit operator override recording WHY
    # a UI-visible ticket may ship without a local-stack E2E artifact (an
    # exempt or genuinely non-UI ticket the heuristic mis-flags). When present
    # with a non-empty ``reason`` the gate passes and logs it.
    dod_e2e_override: "DodE2EOverride"
    # #1426 durable audit marker: a sync writer advanced the ticket to a
    # TERMINAL state (MERGED/DELIVERED) reflecting a real external merge/deploy
    # while the DoD local-E2E gate was unmet. The terminal state is kept (it
    # mirrors reality) but the violation is recorded here rather than silently
    # bypassed, so the gap is auditable.
    dod_e2e_violation: "DodE2EViolation"
    # #1539 evidence the configured ``review_skill`` ran on this ticket. The
    # reviewing-phase gate reads this to refuse a ``reviewing`` attestation
    # that no review-skill execution backs.
    review_skill_run: "ReviewSkillRun"
    # Deep-retrieval evidence backing a ``reviewing`` attestation; read by
    # ``review_context_gate`` (see ``ReviewContext`` below).
    review_context: "ReviewContext"
    # #1829 SHA-bound anti-vacuity proof; read by ``anti_vacuity_gate`` (see
    # ``AntiVacuityAttestation`` below).
    anti_vacuity_attestation: "AntiVacuityAttestation"
    # #1661 per-ticket FixRecord read by ``fix_dod_gate`` at ``mark_delivered`` for
    # ``kind=fix``; written from the agent result envelope by
    # ``agents.fix_record_recorder``. ``fix_record_override`` is the audited escape
    # hatch. Undeclared they are stripped by every ``_extra()`` write-back — the
    # coding-phase write would not survive ``Ticket.test()`` (see ``FixRecord``).
    fix_record: "FixRecord"
    fix_record_override: "FixRecordOverride"
    # PR-08 audited escape hatch for the cross-repo integration-review gate: a
    # ``reason`` for a ≥2-repo ticket exempt from the combined-changeset review;
    # read by ``integration_review_gate`` at ``mark_delivered``.
    integration_review_override: "IntegrationReviewOverride"
    # #2104 delivery-ownership lease: stamped when a hand-dispatched delivery
    # agent (``workspace ticket``) takes the unit, so the loop's scheduling
    # chokepoints skip the auto-planner / duplicate review-arm / global dispatch
    # a directly-implementing external owner never consumes (#2217; see
    # ``ExternalDeliveryLease``).
    external_delivery: "ExternalDeliveryLease"
    # Lightweight audited plan-gate carve-out: a per-ticket marker that this is a
    # trivial mechanical edit whose planning phase is skipped. Stamped via
    # ``trivial_plan_skip.mark_trivial_plan_skip`` with a MANDATORY reason; read
    # by ``check_plan_artifact`` (lets WORK_STARTED→PLAN_RECORDED advance with no
    # PlanArtifact) and ``execute_provision`` (skips the auto-planner). See
    # ``TrivialPlanSkip``.
    trivial_plan_skip: "TrivialPlanSkip"
    # Plan-skipped issue-implementer direct-coding marker: stamped by
    # ``persistence._handle_orchestrator`` when it schedules a ``coding`` task
    # directly on a fresh NOT_STARTED author ticket (no scope/plan phase). Read
    # by ``auto_implement.is_auto_implement`` — the ``Ticket.code_direct``
    # condition that lets a coding-completion advance the FSM from an early
    # state without weakening the normal author flow's plan gate.
    auto_implement: bool
    # #2663 dream-promote = fix-and-merge: a Ticket scheduled to fix a grounded
    # dream gap. ``dream_gap_key`` is the stable gap identity (also the umbrella
    # checkbox marker); ``dream_memory_cluster_key`` links back to the
    # ``ConsolidatedMemory`` row to retire on merge; ``dream_umbrella_url`` is the
    # standing umbrella issue whose checkbox is checked when the fix merges. See
    # ``teatree.loops.dream.umbrella_ledger``.
    dream_gap_key: str
    dream_memory_cluster_key: str
    dream_umbrella_url: str
    # Idempotency stamp for the merged-gap reconcile: the ISO timestamp at which
    # this ticket's merged fix was folded back into the umbrella checkbox and its
    # memory retired. Its PRESENCE is the signal — the reconcile scan skips an
    # already-stamped ticket, so a merged gap is not re-read from the forge on
    # every pass. See ``teatree.loops.dream.umbrella_ledger``.
    dream_gap_reconciled_at: str
    # Pk of the HOST ticket a duplicate gap was folded into. A folded member is retired
    # IGNORED and never reaches MERGED, so the reconcile reads the host's merge instead —
    # without it the member's umbrella box stays open forever. See ``umbrella_ledger``.
    dream_gap_folded_into: int
    # #4776 dream promotion batching: a ticket scheduled to fix an ENTIRE pass's
    # collected gaps (replaces the per-gap ``dream_gap_key`` scheme above for NEW
    # tickets; legacy in-flight tickets keep the old keys). ``dream_gap_batch`` is
    # the full manifest (``[{"gap_key": ..., "cluster_key": ...}, ...]``);
    # ``dream_gap_claimed_delivered`` is the coder/reviewer-recorded subset of gap
    # keys actually delivered, verified independently at review before the
    # reconcile checks a box or retires a memory. See ``teatree.loops.dream.batch_promote``.
    dream_gap_batch: "list[dict[str, str]]"
    dream_gap_claimed_delivered: list[str]
    # #2886: durable pydantic_ai conversation store, keyed by the ``Task.pk`` whose run produced
    # it and selected through ``Task.session_continuation``; see ``teatree.agents.pydantic_ai_resume``.
    pydantic_ai_threads: dict[str, list[object]]
    # #1 dispatch-zone executor contract: metadata the revived correction-zone
    # persistence handlers stamp so the dispatched agent has its context.
    # Codex auto-review: the resolved ``/codex:*`` variant on a reviewer ticket.
    codex_variant: str
    # #3569 Claude self-PR review: the resolved ``claude:*`` variant on a reviewer
    # ticket (the codex fallback path — same shape as ``codex_variant``).
    self_pr_review_variant: str
    # RED CARD corrective action: the ``RedCardSignal`` row + surfaces so the
    # agent can file the enforcement issue and record it via ``link_issue``.
    red_card_signal_id: int
    red_card_signal_kind: str
    red_card_signal_text: str
    red_card_offending_text: str
    # Failed-E2E fix: the spec + test title the fix targets.
    e2e_spec: str
    e2e_test_title: str
    # Skill-drift fix: the drifted repo/file + the finding fingerprint.
    drift_repo: str
    drift_file: str
    drift_fingerprint: str
    # Answerer: the inbound event id + the question detail.
    answer_event_id: int
    answer_detail: str
    # Per-phase attempt budget for scanner-enqueued phases whose deliverable is a
    # field on THIS row (``short_describe`` -> ``short_description``). Such a
    # scanner dedups on the artifact, not on a terminal task, so a phase that
    # never manages to write its field would otherwise re-enqueue every tick
    # forever. ``Ticket.consume_phase_attempt`` bumps the phase's counter on each
    # enqueue and refuses past the ceiling, making the give-up terminal.
    phase_attempts: dict[str, int]
    # #2663: stamped at ticket creation by the reactive slack-answer cycle and read by
    # the answering phase. Undeclared, the first transition stripped it — taking the
    # question text and the work-placed stamp with it on 50 live rows.
    slack_answer: SlackAnswerContext
    # Project-board provenance, re-stamped through ``merge_extra`` on every board sync.
    board_position: int
    board_status: str
    updated_at: str
    # The directive / outer-loop back-link from a synthetic ticket to the row it
    # implements. ``Directive.for_ticket`` resolves this marker BEFORE the reverse FK,
    # so stripping it silently demoted the primary resolver to its fallback.
    directive_id: int
    outer_loop_experiment_id: int
    outer_loop_target: str


class ReviewSkillRun(TypedDict, total=False):
    """Durable evidence that the configured review skill ran (#1539).

    Recorded by ``Ticket.record_review_skill_run`` when the deep-review
    skill named by ``review_skill`` executes. The reviewing-phase gate
    (``teatree.core.gates.review_skill_gate``) consumes it: a ``reviewing``
    attestation is refused unless this records a run of the *currently
    configured* skill.
    """

    skill: str
    at: str


class ReviewContext(TypedDict, total=False):
    """Durable evidence a review retrieved and analyzed the referenced context.

    Reviewing carries the same responsibility as implementing: a verdict from
    the diff alone is not a review. Recorded by
    ``Ticket.record_review_context`` once the reviewer has fetched the
    work item from its source (``work_item``: the Notion/GitLab/tracker URL),
    followed every link in the MR description + ticket, downloaded each
    referenced document (``documents``: spec, design doc, amortization
    schedule, requirement doc), and analyzed them against the
    diff (``analysis``: how the implementation was checked against the
    specified requirements + business rules). The reviewing-phase gate
    (``teatree.core.gates.review_context_gate``) consumes it: when
    ``require_review_context`` is on, entering ``reviewing`` is refused
    until this is recorded.
    """

    work_item: str
    documents: list[str]
    analysis: str
    at: str


class AdequacySection(TypedDict, total=False):
    """One section of a :class:`PlanAdequacy` manifest — substantive OR explicitly-negated.

    ``content`` is the substantive claim (free text for ``design``/``test_strategy``,
    a list of items for ``integration_seams``/``edge_cases``). ``none_reason`` is the
    explicit reasoned negative — the ``no_new_tests`` shape from
    ``anti_vacuity_gate``: a section that genuinely has nothing to declare must SAY
    so with a reason, so silence (both empty) can never pass as "none to declare".
    """

    content: str | list[str]
    none_reason: str


class PlanAdequacy(TypedDict, total=False):
    """A five-section plan-adequacy manifest recorded on a ``PlanArtifact`` (SELFCATCH-3).

    The structural substitute for judging whether a plan is a real plan or a thin
    scope+acceptance spec. Each of the five sections must be substantive OR carry an
    explicit reasoned negative (:class:`AdequacySection`) — a scope-only spec has no
    seams/edge-cases/test-strategy claims to make, so it structurally fails
    ``plan_adequacy.is_adequate``. ``integration_seams.content`` is the list of
    registries/contracts/sibling-paths the change touches; the plan-currency gate
    reads it to decide when a moved target HEAD renders the plan stale.
    ``acceptance_criteria.content`` is the ticket's acceptance-criteria list, which
    ``PlanArtifact.record`` turns into the ticket's ``core.Rubric`` rows — the plan is
    the one home for it, and a reasoned negative there writes no rows and waives nothing
    (the rubric gate's one waiver is the human-authorized ``ticket plan-bypass``).
    """

    design: AdequacySection
    integration_seams: AdequacySection
    edge_cases: AdequacySection
    test_strategy: AdequacySection
    acceptance_criteria: AdequacySection
    # North-star PR-3 debt-delta waivers: the audited escape the ``debt_delta_gate``
    # honours. Each entry (:class:`ApprovedDebt`) names a suppression pattern the
    # plan explicitly approves plus the reason it is acceptable — so a net-new
    # ``noqa`` / ``type-ignore`` / lowered floor ships only against a recorded,
    # reasoned approval, never silently. Absent/empty on a plan that introduces no
    # debt (the common case); it does NOT participate in the five-section
    # :func:`plan_adequacy.is_adequate` check.
    approved_debt: list["ApprovedDebt"]


class ApprovedDebt(TypedDict, total=False):
    """One plan-manifest debt waiver — the audited ``debt_delta_gate`` escape.

    ``pattern`` is matched (case-insensitive substring) against a scanned debt
    introduction's offending line, its signal kind, or its file path; ``reason``
    is the non-empty justification the operator recorded at plan/ratify time. A
    waiver with a blank reason covers nothing — an audited escape must say why.
    """

    pattern: str
    reason: str


class AntiVacuityAttestation(TypedDict, total=False):
    """SHA-bound proof the diff is AC-mapped and its regression tests guard the fix (#1829).

    Recorded by ``Ticket.record_anti_vacuity_attestation`` once the maker has
    run the skilled self-review. The anti-vacuity gate
    (``teatree.core.gates.anti_vacuity_gate``) consumes it to refuse a request-review
    or merge transition when ``require_anti_vacuity_attestation`` is on.

    ``head_sha`` is the full 40-char commit the attestation binds to; the gate
    drops the attestation when the live head moves off it (a new revision must
    be re-attested — mirrors ``MergeClear.reviewed_sha``). ``ac_coverage`` is how
    the diff was mapped against the ticket/spec acceptance criteria (the
    deep-retrieval footprint is the input). ``proven_tests`` are the
    new/modified regression tests for which a revert-fix -> RED proof was
    produced; an empty list means the attester claims the diff adds no new
    regression test, in which case ``no_new_tests`` must be ``True`` so a
    forgotten test can never pass as "none to prove". ``no_new_tests`` is the
    explicit claim that the diff introduces no new regression test (so
    ``proven_tests`` is legitimately empty).
    """

    head_sha: str
    ac_coverage: str
    proven_tests: list[str]
    no_new_tests: bool
    at: str


class RubricGrade(TypedDict, total=False):
    """One verifier's per-criterion grade — the ONE shape every rubric producer speaks.

    Declared here rather than beside any one producer because four surfaces read it
    and a second hand-typed shape is how they drift: the ``rubric-grade`` CLI parser,
    the reviewing agent's ``review_verdict.rubric_grades`` envelope and its JSON
    schema, and :meth:`~teatree.core.models.rubric.Rubric.apply_grades` which stamps
    them. Same reasoning as :class:`FixRecord` above.

    ``rationale`` is required for a PASS and refused-if-absent by
    :meth:`~teatree.core.models.rubric.RubricCriterion.record_grade`, not here — a
    TypedDict cannot express "required only when ``status`` is ``pass``".
    """

    ordinal: int
    status: str
    rationale: str


class FixRecord(TypedDict, total=False):
    """A fix-ticket's Definition-of-Done record (#1661), carried on ``Ticket.extra['fix_record']``.

    Produced by the fixing agent in its result envelope, validated and written by
    ``teatree.agents.fix_record_recorder``, read by ``core.gates.fix_dod_gate`` at
    ``mark_delivered``. Every field is required and non-blank — a manifestation patch
    with no stated root cause is exactly the case the gate exists to catch, so there
    is no partial credit.

    Declared here rather than beside the gate because this declaration is
    load-bearing twice over: it is what ``validated_ticket_extra`` keeps (an
    undeclared key is stripped by every ``_extra()`` write-back, so a record written
    at the coding phase would not survive ``Ticket.test()``), and
    :data:`FIX_RECORD_FIELDS` derived from it is the ONE definition the gate, the
    recorder, the envelope schema and the agent brief all read.
    """

    root_cause: str
    evidence: str
    regression_test: str
    observed_red: str
    recurrence_fingerprint: str


class FixRecordOverride(TypedDict, total=False):
    """Audited escape hatch for a fix-ticket the FixRecord heuristic mis-classifies (#1661).

    ``Ticket.extra['fix_record_override']`` with a non-empty ``reason`` makes the
    gate pass-and-log. Deliberately weaker than the forced-repro gate's
    human-authorized ``ReproWaiver`` — this one is self-authored, which is why it is
    the exception rather than the route (the envelope is the route).
    """

    reason: str


#: The FixRecord fields, in declaration order — THE definition. The gate's required
#: set, the envelope JSON schema, the recorder's validator and the agent brief all
#: derive from this tuple, so a field added to :class:`FixRecord` reaches every one
#: of them and none can drift into a second hand-typed list.
FIX_RECORD_FIELDS: Final[tuple[str, ...]] = tuple(FixRecord.__annotations__)


def fix_record_missing_fields(record: object) -> list[str]:
    """The :data:`FIX_RECORD_FIELDS` *record* leaves absent or blank, in declaration order.

    The one parse shared by the gate that READS the stored record and the recorder
    that VALIDATES the returned one, so producer and consumer cannot drift. A
    non-mapping (or absent) record yields every field — there is no partial credit.
    """
    if not isinstance(record, dict):
        return list(FIX_RECORD_FIELDS)
    return [field for field in FIX_RECORD_FIELDS if not str(record.get(field, "")).strip()]


class IntegrationReviewOverride(TypedDict, total=False):
    """Audited escape hatch for the cross-repo integration-review gate (PR-08).

    ``Ticket.extra['integration_review_override']`` with a non-empty ``reason``
    makes the integration-review gate pass-and-log — for a ≥2-repo ticket whose
    combined changeset was genuinely reviewed out of band (a coordinated hotfix),
    the gate must not hard-trap a legitimate close.
    """

    reason: str


class ExternalDeliveryLease(TypedDict, total=False):
    """TTL'd record that a unit is under active EXTERNAL delivery (#2104).

    Stamped by ``external_delivery.mark_external_delivery`` when a
    hand-dispatched delivery agent takes the ticket via ``workspace ticket`` —
    the external entry the loop's own FSM never uses. The loop consults
    ``external_delivery.under_external_delivery`` at its scheduling chokepoints
    (``execute_provision`` before ``schedule_planning``, and the ``pr_sweep``
    review-arm) and skips the follow-up work a directly-implementing external
    owner will never claim, so the loop stops re-deriving duplicate
    planner/reviewer tasks.

    ``expires_at`` is a UTC ISO timestamp; the lease is self-reaping so a
    crashed external owner cannot permanently wedge the loop's autonomous FSM
    (mirrors the TTL release of ``LoopLease``/``Task`` claims). ``at`` is the
    UTC ISO stamp of when delivery was claimed (audit trail).
    """

    at: str
    expires_at: str


class TrivialPlanSkip(TypedDict, total=False):
    """Audited marker that a trivial AUTHOR ticket's planning phase is skipped.

    Stamped by ``trivial_plan_skip.mark_trivial_plan_skip`` — the lightweight
    sibling of the heavyweight ``plan-bypass`` path — when the operator records
    that a ticket is a trivial mechanical edit (a typo, a one-line constant bump)
    not worth a full planning phase. ``reason`` is mandatory: an unreasoned skip
    is refused before any row is written, so the carve-out is always auditable.

    The marker is read at the two seams the external-delivery predicate also
    uses: ``check_plan_artifact`` accepts it as a satisfying signal (the ticket
    advances WORK_STARTED→PLAN_RECORDED with no ``PlanArtifact`` and no ``--human-authorize``),
    and ``execute_provision`` skips ``schedule_planning`` so the auto-planner is
    never scheduled. ``by`` and ``at`` are the audit trail (who recorded the skip
    and when, a UTC ISO timestamp).
    """

    reason: str
    by: str
    at: str


class BranchCurrencyBlocker(TypedDict, total=False):
    """Durable record of a `ship` defense-in-depth currency refusal (#940).

    Recorded only on a real merge conflict (conflict-only gate): the
    branch trails ``target`` by ``behind`` commits AND the merge would
    conflict in ``conflicting_paths``. Being behind alone never produces
    this record.
    """

    branch: str
    target: str
    behind: int
    conflicting_paths: list[str]


class DodE2EOverride(TypedDict, total=False):
    """Operator escape hatch for the DoD local-E2E gate (#88).

    Records the human-supplied justification for shipping a UI-visible
    ticket without a local-stack E2E artifact, plus who recorded it and
    when, so the bypass is auditable rather than silent.
    """

    reason: str
    by: str
    at: str


class DodE2EViolation(TypedDict, total=False):
    """Durable audit marker for a terminal-state DoD violation (#1426).

    Recorded when automated sync advances a UI-visible ticket to a TERMINAL
    state (MERGED/DELIVERED) reflecting a real external merge/deploy while no
    green local-stack E2E artifact (or override) existed. The terminal state
    is kept because it mirrors reality; this marker makes the unmet DoD
    auditable instead of a silent bypass.
    """

    state: str
    at: str
    detail: str


class E2ERepoEntrySerialized(TypedDict, total=False):
    repo: str
    branch: str
    last_green_sha: str


class E2ELastRunSerialized(TypedDict, total=False):
    result: str
    timestamp: str
    per_repo_shas: dict[str, str]
    # The environment the run executed against: ``"local"`` (teatree-managed
    # on-disk stack), ``"stack"`` (overlay-declared remote local stack), or a
    # deployed target such as ``"dev"`` / ``"qa"``. The DoD gate (#88)
    # requires a *local* green run before a UI-visible ticket may ship — a
    # dev-after-merge run does not satisfy it. Absent on rows recorded before
    # #88; the gate treats a missing env conservatively as not-local.
    env: str
    # Run provenance for vanilla-spec suites (#272): the exact spec path that
    # ran and the overlay-resolved manifest entry id (e.g. a CI lane). Recorded
    # so a run is reproducible from the DB record alone after the workspace is
    # cleaned. Overlay-agnostic plain strings — core never parses them; an
    # overlay with no per-spec manifest leaves both absent. Empty values are
    # dropped rather than stored, so a run with no spec context is
    # indistinguishable from a pre-#272 row.
    spec_path: str
    manifest_entry: str
    # The out-of-repo artifacts root the runner exported as
    # ``T3_E2E_ARTIFACTS_DIR`` for this run (#3331). Recorded so
    # ``write-test-plan --from-seams`` (#3329) can default the artifacts dir to
    # the run's after the workspace is cleaned, instead of the overlay
    # re-deriving it. Absent on rows recorded before the runner owned the path.
    artifacts_dir: str


class E2ERecipeSerialized(TypedDict, total=False):
    """Durable e2e work-item recipe stored under ``Ticket.extra['e2e_recipe']``.

    Keyed by the work item (the Ticket's ``issue_url`` natural key), this is
    the DB-durable provisioning recipe + last-run provenance for #794:
    ``t3 <overlay> e2e run <work-item>``. The teatree DB is the system of
    record — if lost, a baseline is re-established by running against
    current ``origin/main``.
    """

    repos: list[E2ERepoEntrySerialized]
    last_run: E2ELastRunSerialized


class TicketSiblingFields(TypedDict, total=False):
    """Non-``extra`` ``Ticket`` fields a locked ``merge_extra`` co-writes.

    The tracker-sync paths set these alongside ``extra`` in one save;
    ``Ticket.merge_extra(also_set=…)`` keeps that write atomic.
    """

    state: str
    repos: list[str]
    variant: str
    short_description: str


_TICKET_EXTRA_KEYS = frozenset(TicketExtra.__annotations__)

#: Announced (model, key) pairs. Process-wide: these validators sit on hot read paths, so a
#: per-call warning would drown the log — but a strip nobody ever hears about is how #4152's
#: revival cap read zero for months and #2663's Slack context vanished from 50 rows.
_WARNED_STRIPPED_KEYS: set[tuple[str, str]] = set()


def reset_stripped_key_warnings() -> None:
    """Forget which keys were announced — for tests, so the once-ness never crosses one."""
    _WARNED_STRIPPED_KEYS.clear()


def _warn_stripped(model: str, dropped: Iterable[str]) -> None:
    """Announce each undeclared key once, naming the TypedDict that would have kept it."""
    for key in sorted(dropped):
        if (model, key) in _WARNED_STRIPPED_KEYS:
            continue
        _WARNED_STRIPPED_KEYS.add((model, key))
        logger.warning(
            "%s.extra: dropping undeclared key %r — declare it on %sExtra or the next transition strips it.",
            model,
            key,
            model,
        )


def validated_ticket_extra(raw: dict | None) -> TicketExtra:
    if not raw:
        return TicketExtra()
    _warn_stripped("Ticket", raw.keys() - _TICKET_EXTRA_KEYS)
    return TicketExtra(**{k: v for k, v in raw.items() if k in _TICKET_EXTRA_KEYS})


class WorktreeSiblingFields(TypedDict, total=False):
    """Non-``extra`` ``Worktree`` fields a locked ``merge_extra`` co-writes.

    The intake resolver re-points a reused row's ``branch`` alongside its
    ``extra.worktree_path``; ``Worktree.merge_extra(also_set=…)`` keeps that
    write in the same locked UPDATE. Mirrors :class:`TicketSiblingFields`.
    """

    branch: str


class WorktreeExtra(TypedDict, total=False):
    worktree_path: str
    clone_path: str
    services: list[str]
    urls: dict[str, str]
    pids: dict[str, int]
    failed_services: list[str]
    db_refreshed_at: str
    db_import_failures: int
    setup_hook: str
    # Written by the start runner alongside ``services`` in one locked update, and
    # by the provision runner at the end of a provision. Both were absent from this
    # TypedDict, so ``_WORKTREE_EXTRA_KEYS`` filtered them straight back out of
    # every ``get_extra()`` read — declared here so the accessor keeps them.
    ports: Ports
    provision_report: "ProvisionReportDict"
    # #2227 Explicit operator pin: when true the idle-stack reaper never reaps
    # this worktree, regardless of idleness — the manual escape hatch alongside
    # the active-delivery-lease and recent-E2E-run KEEP guards.
    reaper_pinned: bool


# Known keys for WorktreeExtra — used by get_extra() to filter stale data
_WORKTREE_EXTRA_KEYS = frozenset(WorktreeExtra.__annotations__)


def validated_worktree_extra(raw: dict | None) -> WorktreeExtra:
    """Coerce a raw dict (from JSONField) into a typed WorktreeExtra.

    Returns only recognized keys, announcing the dropped ones once each.
    Handles ``None`` gracefully (returns empty dict).
    """
    if not raw:
        return WorktreeExtra()
    _warn_stripped("Worktree", raw.keys() - _WORKTREE_EXTRA_KEYS)
    return WorktreeExtra(**{k: v for k, v in raw.items() if k in _WORKTREE_EXTRA_KEYS})
