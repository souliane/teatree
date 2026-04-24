"""Concern-based backend protocols.

Each protocol defines a capability that teatree needs from external services.
Overlays configure which implementation to use via Django settings.

A single class can satisfy multiple protocols when the platform provides
multiple concerns (e.g. GitLab provides code hosting, CI, and issue tracking).
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from teatree.core.sync import RawAPIDict


@dataclass(frozen=True, slots=True)
class PullRequestSpec:
    """Fields needed to open a pull/merge request on a CodeHost."""

    repo: str
    branch: str
    title: str
    description: str
    target_branch: str = ""
    labels: list[str] = field(default_factory=list)
    assignee: str = ""
    draft: bool = False


@runtime_checkable
class CodeHost(Protocol):
    """Create and list pull/merge requests."""

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict: ...  # pragma: no branch

    def current_user(self) -> str: ...  # pragma: no branch

    def list_open_prs(self, repo: str, author: str) -> list[RawAPIDict]: ...  # pragma: no branch

    def post_mr_note(self, *, repo: str, mr_iid: int, body: str) -> RawAPIDict: ...  # pragma: no branch

    def update_mr_note(
        self,
        *,
        repo: str,
        mr_iid: int,
        note_id: int,
        body: str,
    ) -> RawAPIDict: ...  # pragma: no branch

    def list_mr_notes(self, *, repo: str, mr_iid: int) -> list[RawAPIDict]: ...  # pragma: no branch

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict: ...  # pragma: no branch


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
class IssueTracker(Protocol):
    """Fetch issue details from a project tracker."""

    def get_issue(self, issue_url: str) -> RawAPIDict: ...  # pragma: no branch


@runtime_checkable
class ChatNotifier(Protocol):
    """Send notifications to a team chat channel."""

    def send(self, *, channel: str, text: str) -> RawAPIDict: ...  # pragma: no branch


@runtime_checkable
class ErrorTracker(Protocol):
    """Fetch error/issue data from an error tracking service."""

    def get_top_issues(self, *, project: str, limit: int = 10) -> list[RawAPIDict]: ...  # pragma: no branch
