"""Tests for backend protocol structural typing."""

from teetree.backends.protocols import ChatNotifier, CIService, CodeHost, ErrorTracker, IssueTracker


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


def test_code_host_protocol_is_structural() -> None:
    class MyCodeHost:
        def create_pr(
            self,
            *,
            repo: str,
            branch: str,
            title: str,
            description: str,
            target_branch: str = "",
        ) -> dict[str, object]:
            return {}

        def list_open_prs(self, repo: str, author: str) -> list[dict[str, object]]:
            return []

        def post_mr_note(self, *, repo: str, mr_iid: int, body: str) -> dict[str, object]:
            return {}

    assert isinstance(MyCodeHost(), CodeHost)


def test_issue_tracker_protocol_is_structural() -> None:
    class MyIssueTracker:
        def get_issue(self, issue_url: str) -> dict[str, object]:
            return {}

    assert isinstance(MyIssueTracker(), IssueTracker)


def test_chat_notifier_protocol_is_structural() -> None:
    class MyChatNotifier:
        def send(self, *, channel: str, text: str) -> dict[str, object]:
            return {}

    assert isinstance(MyChatNotifier(), ChatNotifier)


def test_error_tracker_protocol_is_structural() -> None:
    class MyErrorTracker:
        def get_top_issues(self, *, project: str, limit: int = 10) -> list[dict[str, object]]:
            return []

    assert isinstance(MyErrorTracker(), ErrorTracker)


def test_non_conforming_class_is_not_ci_service() -> None:
    class NotACIService:
        pass

    assert not isinstance(NotACIService(), CIService)


def test_non_conforming_class_is_not_code_host() -> None:
    class NotACodeHost:
        pass

    assert not isinstance(NotACodeHost(), CodeHost)


def test_non_conforming_class_is_not_issue_tracker() -> None:
    class NotAnIssueTracker:
        pass

    assert not isinstance(NotAnIssueTracker(), IssueTracker)


def test_non_conforming_class_is_not_chat_notifier() -> None:
    class NotAChatNotifier:
        pass

    assert not isinstance(NotAChatNotifier(), ChatNotifier)


def test_non_conforming_class_is_not_error_tracker() -> None:
    class NotAnErrorTracker:
        pass

    assert not isinstance(NotAnErrorTracker(), ErrorTracker)
