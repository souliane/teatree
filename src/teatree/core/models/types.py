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


class BranchCurrencyBlocker(TypedDict, total=False):
    """Durable record of a `ship` defense-in-depth currency refusal (#940)."""

    branch: str
    target: str
    behind: int


class E2ERepoEntrySerialized(TypedDict, total=False):
    repo: str
    branch: str
    last_green_sha: str


class E2ELastRunSerialized(TypedDict, total=False):
    result: str
    timestamp: str
    per_repo_shas: dict[str, str]


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
