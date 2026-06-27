"""Tests for backend protocol structural typing."""

from teatree.core.backend_protocols import (
    ApprovalState,
    CIService,
    CodeHostBackend,
    ForgeMergeResult,
    MessageSpec,
    MessagingBackend,
    PrMergeState,
    PrOpenState,
    PullRequestSpec,
    ReviewState,
    UploadVerification,
)


class _FakeCIService:
    def cancel_pipelines(self, *, project: str, ref: str) -> list[int]:
        _ = (project, ref)
        return []

    def fetch_pipeline_errors(self, *, project: str, ref: str) -> list[str]:
        _ = (project, ref)
        return []

    def fetch_failed_tests(self, *, project: str, ref: str) -> list[str]:
        _ = (project, ref)
        return []

    def trigger_pipeline(
        self,
        *,
        project: str,
        ref: str,
        variables: dict[str, str] | None = None,
    ) -> dict[str, object]:
        _ = (project, ref, variables)
        return {}

    def quality_check(self, *, project: str, ref: str) -> dict[str, object]:
        _ = (project, ref)
        return {}


class _FakeCodeHost:
    def create_pr(self, spec: PullRequestSpec) -> dict[str, object]:
        _ = spec
        return {}

    def current_user(self) -> str:
        return ""

    def list_my_prs(self, *, author: str) -> list[dict[str, object]]:
        _ = author
        return []

    def list_my_merged_prs(self, *, author: str) -> list[dict[str, object]]:
        _ = author
        return []

    def list_review_requested_prs(self, *, reviewer: str) -> list[dict[str, object]]:
        _ = reviewer
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        _ = pr_url
        return PrOpenState.UNKNOWN

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return ""

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> dict[str, object]:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> dict[str, object]:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[dict[str, object]]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
        _ = (repo, filepath)
        return {}

    def verify_upload(self, *, repo: str, upload: dict[str, object]) -> UploadVerification:
        _ = (repo, upload)
        return UploadVerification(ok=True, embed_url="")

    def repo_for_issue_url(self, issue_url: str) -> str:
        _ = issue_url
        return ""

    def get_issue(self, issue_url: str) -> dict[str, object]:
        _ = issue_url
        return {}

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, object]:
        _ = (issue_url, body)
        return {}

    def post_issue_comment(self, *, issue_url: str, body: str) -> dict[str, object]:
        _ = (issue_url, body)
        return {}

    def list_issue_comments(self, *, issue_url: str) -> list[dict[str, object]]:
        _ = issue_url
        return []

    def update_issue_comment(self, *, issue_url: str, comment_id: int, body: str) -> dict[str, object]:
        _ = (issue_url, comment_id, body)
        return {}

    def delete_issue_comment(self, *, issue_url: str, comment_id: int) -> dict[str, object]:
        _ = (issue_url, comment_id)
        return {}

    def list_assigned_issues(self, *, assignee: str) -> list[dict[str, object]]:
        _ = assignee
        return []

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, object]:
        _ = (repo, title, body, labels)
        return {}

    def create_sub_issue(
        self,
        *,
        parent_url: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        child_type: str = "Task",
    ) -> dict[str, object]:
        _ = (parent_url, title, body, labels, child_type)
        return {}

    def search_open_issues(self, *, repo: str, query: str) -> list[dict[str, object]]:
        _ = (repo, query)
        return []

    def close_issue(self, *, issue_url: str, comment: str = "") -> dict[str, object]:
        _ = (issue_url, comment)
        return {}

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        _ = (repo, pr_iid)
        return ApprovalState(approvals_left=0, approved_by=[], unresolved_resolvable=0)

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        _ = (slug, pr_id)
        return ""

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        _ = (slug, pr_id)
        return PrMergeState(state="", merge_commit_oid="")

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        _ = (slug, pr_id)
        return False

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[dict[str, object]]:
        _ = (slug, pr_id)
        return []

    def fetch_required_status_check_contexts(self, *, slug: str, pr_id: int) -> list[dict[str, object]]:
        _ = (slug, pr_id)
        return []

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        _ = (slug, pr_id)
        return []

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        _ = (slug, pr_id, expected_head_oid)
        return ForgeMergeResult(returncode=0, stdout="", stderr="", merged_sha="")


class _FakeMessaging:
    def fetch_mentions(self, *, since: str = "") -> list[dict[str, object]]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[dict[str, object]]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[dict[str, object]]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> dict[str, object]:
        _ = (channel, ts)
        return {}

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[dict[str, object]]:
        _ = (channel, thread_ts)
        return []

    def fetch_channel_history(self, *, channel: str, limit: int = 50) -> list[dict[str, object]]:
        _ = (channel, limit)
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> dict[str, object]:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        _ = (channel, ts)
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        _ = (channel, ts, emoji)
        return {}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        _ = (channel, text, thread_ts)
        return {}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> dict[str, object]:
        return {}

    def post_audio_dm(
        self, *, channel: str, filepath: str, text: str, thread_ts: str = "", title: str = ""
    ) -> dict[str, object]:
        _ = (channel, filepath, text, thread_ts, title)
        return {}


def test_ci_service_protocol_is_structural() -> None:
    assert isinstance(_FakeCIService(), CIService)


def test_code_host_backend_protocol_is_structural() -> None:
    assert isinstance(_FakeCodeHost(), CodeHostBackend)


def test_messaging_backend_protocol_is_structural() -> None:
    assert isinstance(_FakeMessaging(), MessagingBackend)


def test_non_conforming_class_is_not_ci_service() -> None:
    class NotACIService:
        pass

    assert not isinstance(NotACIService(), CIService)


def test_non_conforming_class_is_not_code_host() -> None:
    class NotACodeHost:
        pass

    assert not isinstance(NotACodeHost(), CodeHostBackend)


def test_non_conforming_class_is_not_messaging_backend() -> None:
    class NotAMessaging:
        pass

    assert not isinstance(NotAMessaging(), MessagingBackend)


def test_message_spec_default_thread_ts() -> None:
    spec = MessageSpec(channel="C123", text="hello")
    assert spec.thread_ts == ""
