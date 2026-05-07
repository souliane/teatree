"""Tests for backend protocol structural typing."""

from teatree.backends.protocols import (
    CIService,
    CodeHostBackend,
    MessageSpec,
    MessagingBackend,
    PullRequestSpec,
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

    def list_review_requested_prs(self, *, reviewer: str) -> list[dict[str, object]]:
        _ = reviewer
        return []

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

    def get_issue(self, issue_url: str) -> dict[str, object]:
        _ = issue_url
        return {}

    def list_assigned_issues(self, *, assignee: str) -> list[dict[str, object]]:
        _ = assignee
        return []


class _FakeMessaging:
    def fetch_mentions(self, *, since: str = "") -> list[dict[str, object]]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[dict[str, object]]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> dict[str, object]:
        _ = (channel, ts, text)
        return {}

    def react(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


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
