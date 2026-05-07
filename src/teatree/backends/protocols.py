"""Concern-based backend protocols.

Each protocol defines a capability that teatree needs from external services.
Overlays declare which implementation to load via ``OverlayConfig`` fields
(``code_host``, ``messaging_backend``); ``backends.loader`` resolves the choice.

A single class can satisfy multiple protocols when the platform provides
multiple concerns (e.g. GitLab provides code hosting and CI in one client).
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from teatree.core.sync import RawAPIDict


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


@runtime_checkable
class CodeHostBackend(Protocol):
    """Pull/merge requests + issue fetch — the canonical code-host concern.

    PR is the canonical term in core; GitLab implementations translate
    MR ↔ PR at the API edge. ``repo`` + ``pr_iid`` is the natural unit on
    both APIs (GitLab ``merge_requests/<iid>``, GitHub ``pulls/<number>``).
    """

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict: ...  # pragma: no branch

    def current_user(self) -> str: ...  # pragma: no branch

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]: ...  # pragma: no branch

    def list_review_requested_prs(self, *, reviewer: str) -> list[RawAPIDict]: ...  # pragma: no branch

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

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]: ...  # pragma: no branch


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

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict: ...  # pragma: no branch

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict: ...  # pragma: no branch

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict: ...  # pragma: no branch

    def resolve_user_id(self, handle: str) -> str: ...  # pragma: no branch
