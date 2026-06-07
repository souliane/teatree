"""Concern-based backend protocols.

Each protocol defines a capability that teatree needs from external services.
Overlays declare which implementation to load via ``OverlayConfig`` fields
(``code_host``, ``messaging_backend``); ``backends.loader`` resolves the choice.

A single class can satisfy multiple protocols when the platform provides
multiple concerns (e.g. GitLab provides code hosting and CI in one client).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypedDict, runtime_checkable

from teatree.types import RawAPIDict


class BackendResolutionError(Exception):
    """No code-host backend resolves for a repo's actual origin host.

    Raised by the per-repo host resolver when a repo lives on a forge
    whose credentials the active overlay has not configured (e.g. a
    GitLab-hosted repo with no GitLab token). Surfacing this BEFORE the
    PR-creation attempt replaces the raw ``gh``/``glab`` GraphQL error
    ("Could not resolve to a Repository") that previously was the first
    signal of a mismatched forge selection (#2025).
    """


class ApprovalState(TypedDict):
    """Backend-resolved approval snapshot for a single PR/MR (#936).

    ``approvals_left`` is the remaining count of required approvals — 0 when the
    forge-side approval threshold is satisfied. ``approved_by`` is the list of
    approver usernames in approval order. ``unresolved_resolvable`` counts open
    discussion threads whose ``resolvable`` flag is true (i.e. would block a
    merge under the upstream's "must resolve" policy) — distinct from system
    note threads or non-resolvable comments.
    """

    approvals_left: int
    approved_by: list[str]
    unresolved_resolvable: int


class ReviewState(StrEnum):
    """A reviewer's current state on a single pull/merge request.

    Used by ``CodeHostBackend.get_review_state`` and by
    ``ReviewerPrsScanner`` to detect approval dismissals — e.g. when a
    forge invalidates a prior approval on force-push, or when a reviewer
    is re-requested after being dismissed.
    """

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    DISMISSED = "dismissed"
    # The reviewer concluded an external review with no postable/approvable
    # action (e.g. a bot MR there is nothing to comment on or approve).
    # Distinct from APPROVED so the dedup never hides a future genuine
    # review, yet terminal so the reviewing task stops re-queueing (#1077).
    REVIEWED_NO_ACTION = "reviewed_no_action"


class PrOpenState(StrEnum):
    """Whether a pull/merge request is genuinely still open on the forge.

    Used by ``CodeHostBackend.get_pr_open_state`` so the orphan-task sweep
    (``ReviewerPrsScanner._orphaned_task_signals``) can confirm a reviewing
    task's PR is really MERGED/CLOSED before reaping it — absence from a
    reviewer-assignment scan is NOT proof the PR closed (#1074). ``UNKNOWN``
    is the fail-open value: any auth error, network failure, unparsable URL,
    or unrecognised payload maps here, and the sweep never reaps on UNKNOWN.
    """

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PullRequestSpec:
    """Fields needed to open a pull/merge request on a CodeHostBackend."""

    repo: str
    branch: str
    title: str
    description: str
    target_branch: str = ""
    labels: list[str] = field(default_factory=list)
    assignee: str = ""
    draft: bool = False


@dataclass(frozen=True, slots=True)
class MessageSpec:
    """Fields for an outgoing chat message."""

    channel: str
    text: str
    thread_ts: str = ""


@dataclass(frozen=True, slots=True)
class PrMergeState:
    """The PR/MR's merge state from the forge — used for the §928 reconciliation.

    ``state`` is the forge's PR state (``OPEN`` / ``MERGED`` / ``CLOSED``, always
    upper-cased so ``is_merged`` works across GitHub and GitLab);
    ``merge_commit_oid`` is the resulting squash/merge commit when the PR is
    already merged (else ``""``).
    """

    state: str
    merge_commit_oid: str

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"


_ROLLUP_QUERY_FAILED_KEY = "_teatree_rollup_query_failed"
ROLLUP_QUERY_FAILED: "RawAPIDict" = {_ROLLUP_QUERY_FAILED_KEY: True}
"""Sentinel rollup entry — the backend could not read the live checks rollup.

``fetch_required_checks_rollup`` returns ``[ROLLUP_QUERY_FAILED]`` when the forge
query itself failed (non-zero rc / malformed / non-list payload), distinct from
an empty list (no required checks → green). Core's classifier treats the sentinel
as ``failed`` so a transport failure is never mistaken for "no checks to satisfy".
"""


def rollup_query_failed(rollup: "list[RawAPIDict]") -> bool:
    """True iff *rollup* carries the :data:`ROLLUP_QUERY_FAILED` sentinel."""
    return any(entry.get(_ROLLUP_QUERY_FAILED_KEY) is True for entry in rollup)


@dataclass(frozen=True, slots=True)
class ForgeMergeResult:
    """Raw outcome of a backend bound-squash-merge — core does the classification.

    The backend performs the I/O and returns the unclassified
    ``(returncode, stdout, stderr)`` plus the ``merged_sha`` it parsed from a
    successful response. Core's :mod:`teatree.core.merge.execution` runs the
    transient / head-moved / policy-refusal classification on these fields and
    raises the typed errors with the exact f-strings — keeping byte-for-byte
    error parity while the transport lives in the backend.
    """

    returncode: int
    stdout: str
    stderr: str
    merged_sha: str = ""


@runtime_checkable
class CodeHostBackend(Protocol):
    """Pull/merge requests + issue fetch — the canonical code-host concern.

    PR is the canonical term in core; GitLab implementations translate
    MR ↔ PR at the API edge. ``repo`` + ``pr_iid`` is the natural unit on
    both APIs (GitLab ``merge_requests/<iid>``, GitHub ``pulls/<number>``).
    """

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict: ...  # pragma: no branch

    def current_user(self) -> str: ...  # pragma: no branch

    def list_my_prs(
        self,
        *,
        author: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]: ...  # pragma: no branch

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]: ...  # pragma: no branch

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState: ...  # pragma: no branch

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState: ...  # pragma: no branch

    def get_pr_author(self, *, pr_url: str) -> str: ...  # pragma: no branch

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict: ...  # pragma: no branch

    def update_pr_comment(
        self,
        *,
        repo: str,
        pr_iid: int,
        comment_id: int,
        body: str,
    ) -> RawAPIDict: ...  # pragma: no branch

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]: ...  # pragma: no branch

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict: ...  # pragma: no branch

    def get_issue(self, issue_url: str) -> RawAPIDict: ...  # pragma: no branch

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict: ...  # pragma: no branch

    def list_issue_comments(self, *, issue_url: str) -> list[RawAPIDict]: ...  # pragma: no branch

    def update_issue_comment(
        self,
        *,
        issue_url: str,
        comment_id: int,
        body: str,
    ) -> RawAPIDict: ...  # pragma: no branch

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]: ...  # pragma: no branch

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> RawAPIDict: ...  # pragma: no branch

    def create_sub_issue(
        self,
        *,
        parent_url: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        child_type: str = "Task",
    ) -> RawAPIDict: ...  # pragma: no branch

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]: ...  # pragma: no branch

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState: ...  # pragma: no branch

    # §17.4.3 merge-RPC surface — raw I/O; ``teatree.core.merge.execution``
    # keeps every verdict/transient/head-moved classification and error
    # f-string so the keystone path stays byte-for-byte identical across
    # forges. The raw ``gh``/``glab`` argv is the canonical chokepoint home
    # here (the argv-ban chokepoint itself awaits the #1890 match-kind).

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str: ...  # pragma: no branch

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState: ...  # pragma: no branch

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool: ...  # pragma: no branch

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]: ...  # pragma: no branch

    def merge_pr_squash_bound(
        self,
        *,
        slug: str,
        pr_id: int,
        expected_head_oid: str,
    ) -> ForgeMergeResult: ...  # pragma: no branch


@runtime_checkable
class CIService(Protocol):
    """Interact with CI/CD pipelines — cancel, fetch logs/tests, trigger."""

    def cancel_pipelines(self, *, project: str, ref: str) -> list[int]: ...  # pragma: no branch

    def fetch_pipeline_errors(self, *, project: str, ref: str) -> list[str]: ...  # pragma: no branch

    def fetch_failed_tests(self, *, project: str, ref: str) -> list[str]: ...  # pragma: no branch

    def trigger_pipeline(
        self,
        *,
        project: str,
        ref: str,
        variables: dict[str, str] | None = None,
    ) -> RawAPIDict: ...  # pragma: no branch

    def quality_check(self, *, project: str, ref: str) -> RawAPIDict: ...  # pragma: no branch


@runtime_checkable
class MessagingBackend(Protocol):
    """Messaging — mentions, DMs, posts, reactions, user lookup.

    The single Protocol covers both inbound (fetch_mentions, fetch_dms) and
    outbound (post_message, post_reply, react) concerns plus user-id
    resolution for routing.
    """

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]: ...  # pragma: no branch

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]: ...  # pragma: no branch

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]: ...  # pragma: no branch

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict: ...  # pragma: no branch

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[RawAPIDict]: ...  # pragma: no branch

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict: ...  # pragma: no branch

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict: ...  # pragma: no branch

    def open_dm(self, user_id: str) -> str: ...  # pragma: no branch

    def get_permalink(self, *, channel: str, ts: str) -> str: ...  # pragma: no branch

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict: ...  # pragma: no branch

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict: ...  # pragma: no branch

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict: ...  # pragma: no branch

    def resolve_user_id(self, handle: str) -> str: ...  # pragma: no branch

    def auth_test(self) -> RawAPIDict: ...  # pragma: no branch

    def post_audio_dm(
        self,
        *,
        channel: str,
        filepath: str,
        text: str,
        thread_ts: str = "",
        title: str = "",
    ) -> RawAPIDict: ...  # pragma: no branch
