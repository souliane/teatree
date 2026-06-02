from typing import TypedDict

type Ports = dict[str, int]


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


class TicketExtra(TypedDict, total=False):
    tests_passed: bool
    pr_urls: list[str]
    # #1263: per-branch PR URL index so a reused-ticket multi-workstream
    # ship can tell whether the *current* invoking branch's PR exists,
    # without short-circuiting on the truthiness of the shared ``pr_urls``.
    pr_url_by_branch: dict[str, str]
    prs: dict[str, PREntrySerialized]
    pr_title_override: str
    ship_invoking_branch: str
    ignored_from: str
    reopened_from: str
    visual_qa: VisualQASummary
    branch: str
    description: str
    provision: dict[str, str]
    shipping_skipped: str
    tracker_status: str
    issue_title: str
    labels: list[str]
    auto_started: bool
    reviewed_sha: str
    last_review_state: str
    retro_scheduled: bool
    tracker_404: bool
    more_prs_coming: bool
    e2e_recipe: "E2ERecipeSerialized"
    # #940 branch-currency gate state: post-merge SHA the cold reviewer
    # must attest, and a durable refusal entry when the ship-time
    # defense-in-depth re-check rejects the push.
    target_branch: str
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
    # Deep-retrieval evidence: the ticket/work-item was fetched from its
    # source and the referenced documents were downloaded + analyzed against
    # the diff before the ``reviewing`` attestation. The reviewing-phase gate
    # (``teatree.core.review_context_gate``) reads this to refuse a verdict
    # formed from the diff alone.
    review_context: "ReviewContext"


class ReviewSkillRun(TypedDict, total=False):
    """Durable evidence that the configured review skill ran (#1539).

    Recorded by ``Ticket.record_review_skill_run`` when the deep-review
    skill named by ``review_skill`` executes. The reviewing-phase gate
    (``teatree.core.review_skill_gate``) consumes it: a ``reviewing``
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
    referenced document (``documents``: spec, design doc, amortization /
    Tilgungsplan schedule, requirement doc), and analyzed them against the
    diff (``analysis``: how the implementation was checked against the
    specified requirements + business rules). The reviewing-phase gate
    (``teatree.core.review_context_gate``) consumes it: when
    ``require_review_context`` is on, entering ``reviewing`` is refused
    until this is recorded.
    """

    work_item: str
    documents: list[str]
    analysis: str
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
    # local stack) or ``"dev"`` (deployed dev environment). The DoD gate (#88)
    # requires a *local* green run before a UI-visible ticket may ship — a
    # dev-after-merge run does not satisfy it. Absent on rows recorded before
    # #88; the gate treats a missing env conservatively as not-local.
    env: str


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


_TICKET_EXTRA_KEYS = frozenset(TicketExtra.__annotations__)


def validated_ticket_extra(raw: dict | None) -> TicketExtra:
    if not raw:
        return TicketExtra()
    return TicketExtra(**{k: v for k, v in raw.items() if k in _TICKET_EXTRA_KEYS})


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


# Known keys for WorktreeExtra — used by get_extra() to filter stale data
_WORKTREE_EXTRA_KEYS = frozenset(WorktreeExtra.__annotations__)


def validated_worktree_extra(raw: dict | None) -> WorktreeExtra:
    """Coerce a raw dict (from JSONField) into a typed WorktreeExtra.

    Returns only recognized keys, silently dropping unknown ones.
    Handles ``None`` gracefully (returns empty dict).
    """
    if not raw:
        return WorktreeExtra()
    return WorktreeExtra(**{k: v for k, v in raw.items() if k in _WORKTREE_EXTRA_KEYS})
