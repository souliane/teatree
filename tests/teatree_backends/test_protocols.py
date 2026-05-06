"""Tests for backend protocol structural typing."""

from teatree.backends.protocols import (
    CIService,
    CodeHostBackend,
    MessageSpec,
    MessagingBackend,
    PullRequestSpec,
)


def test_ci_service_protocol_is_structural() -> None:
    class MyCIService:
        def cancel_pipelines(self, *, project: str, ref: str) -> list[int]:
            return []

        def fetch_pipeline_errors(self, *, project: str, ref: str) -> list[str]:
            return []

        def fetch_failed_tests(self, *, project: str, ref: str) -> list[str]:
            return []

        def trigger_pipeline(
            self,
            *,
            project: str,
            ref: str,
            variables: dict[str, str] | None = None,
        ) -> dict[str, object]:
            return {}

        def quality_check(self, *, project: str, ref: str) -> dict[str, object]:
            return {}

    assert isinstance(MyCIService(), CIService)


def test_code_host_backend_protocol_is_structural() -> None:
    class MyCodeHost:
        def create_pr(self, spec: PullRequestSpec) -> dict[str, object]:
            _ = spec
            return {}

        def current_user(self) -> str:
            return ""

        def list_open_prs(self, repo: str, author: str) -> list[dict[str, object]]:
            return []

        def list_my_open_prs(self, author: str) -> list[dict[str, object]]:
            return []

        def post_mr_note(self, *, repo: str, mr_iid: int, body: str) -> dict[str, object]:
            return {}

        def update_mr_note(self, *, repo: str, mr_iid: int, note_id: int, body: str) -> dict[str, object]:
            return {}

        def list_mr_notes(self, *, repo: str, mr_iid: int) -> list[dict[str, object]]:
            return []

        def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
            return {}

        def get_issue(self, issue_url: str) -> dict[str, object]:
            return {}

    assert isinstance(MyCodeHost(), CodeHostBackend)


def test_messaging_backend_protocol_is_structural() -> None:
    class MyMessaging:
        def fetch_mentions(self, *, since: str = "") -> list[dict[str, object]]:
            return []

        def fetch_dms(self, *, since: str = "") -> list[dict[str, object]]:
            return []

        def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> dict[str, object]:
            return {}

        def post_reply(self, *, channel: str, ts: str, text: str) -> dict[str, object]:
            return {}

        def react(self, *, channel: str, ts: str, emoji: str) -> dict[str, object]:
            return {}

        def resolve_user_id(self, handle: str) -> str:
            return ""

    assert isinstance(MyMessaging(), MessagingBackend)


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
