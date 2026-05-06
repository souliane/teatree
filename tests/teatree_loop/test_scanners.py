"""Tests for the loop scanners — pure-Python signal collectors."""

from pathlib import Path
from unittest.mock import MagicMock

from teatree.backends.protocols import CodeHostBackend, MessagingBackend
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.notion_view import NotionViewScanner
from teatree.loop.scanners.review_channels import ReviewChannelsScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner


class TestMyPrsScanner:
    def test_returns_empty_when_user_unknown(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = ""
        assert MyPrsScanner(host=host).scan() == []

    def test_failed_pipeline_yields_action_needed_signal(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = "alice"
        host.list_my_open_prs.return_value = [
            {
                "iid": 7,
                "title": "Fix thing",
                "web_url": "https://gitlab/x/-/merge_requests/7",
                "head_pipeline": {"status": "failed"},
            }
        ]
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]
        assert "Fix thing" in signals[0].summary

    def test_unresolved_notes_yields_draft_notes_signal(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = "alice"
        host.list_my_open_prs.return_value = [{"iid": 8, "title": "WIP", "web_url": "x", "user_notes_count": 3}]
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.draft_notes"]

    def test_clean_pr_yields_open_signal(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = "alice"
        host.list_my_open_prs.return_value = [
            {"iid": 9, "title": "Done", "web_url": "x", "head_pipeline": {"status": "success"}}
        ]
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]


class TestReviewerPrsScanner:
    def test_unreviewed_first_pass_emits_signal(self, tmp_path: Path) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.list_open_prs.return_value = [{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "abc"}]
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]

    def test_new_sha_after_review_emits_new_sha(self, tmp_path: Path) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.list_open_prs.return_value = [{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "newer"}]
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        scanner.mark_reviewed(url="https://gitlab/x/-/merge_requests/3", sha="older")
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.new_sha"]

    def test_already_reviewed_emits_nothing(self, tmp_path: Path) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.list_open_prs.return_value = [{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "same"}]
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        scanner.mark_reviewed(url="https://gitlab/x/-/merge_requests/3", sha="same")
        assert scanner.scan() == []


class TestSlackMentionsScanner:
    def test_emits_one_signal_per_event(self, tmp_path: Path) -> None:
        backend = MagicMock(spec=MessagingBackend)
        backend.fetch_mentions.return_value = [{"ts": "1.0", "text": "hey"}]
        backend.fetch_dms.return_value = [{"ts": "2.0", "text": "DM"}]
        signals = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan()
        kinds = sorted(s.kind for s in signals)
        assert kinds == ["slack.dm", "slack.mention"]

    def test_empty_when_no_events(self, tmp_path: Path) -> None:
        backend = MagicMock(spec=MessagingBackend)
        backend.fetch_mentions.return_value = []
        backend.fetch_dms.return_value = []
        assert SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan() == []


class TestReviewChannelsScanner:
    def test_extracts_pr_url_from_mention(self) -> None:
        backend = MagicMock(spec=MessagingBackend)
        backend.fetch_mentions.return_value = [
            {"ts": "1.0", "text": "review please https://gitlab.com/group/proj/-/merge_requests/42"}
        ]
        backend.fetch_dms.return_value = []
        signals = ReviewChannelsScanner(backend=backend).scan()
        assert [s.kind for s in signals] == ["review_channel.request"]
        assert signals[0].payload["url"] == "https://gitlab.com/group/proj/-/merge_requests/42"

    def test_no_url_no_signal(self) -> None:
        backend = MagicMock(spec=MessagingBackend)
        backend.fetch_mentions.return_value = [{"ts": "1.0", "text": "just a chat"}]
        backend.fetch_dms.return_value = []
        assert ReviewChannelsScanner(backend=backend).scan() == []


class TestNotionViewScanner:
    def test_no_op_when_client_missing(self) -> None:
        assert NotionViewScanner(client=None).scan() == []

    def test_emits_one_signal_per_unrouted_item(self) -> None:
        client = MagicMock()
        client.list_unrouted.return_value = [{"title": "Spec for API"}]
        signals = NotionViewScanner(client=client).scan()
        assert [s.kind for s in signals] == ["notion.unrouted"]


class TestAssignedIssuesScanner:
    def test_filters_by_ready_label(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = "alice"

        def fake_list(_host: object, _author: str) -> list[dict[str, object]]:
            return [
                {"web_url": "x", "title": "ready", "labels": ["ready"]},
                {"web_url": "y", "title": "draft", "labels": ["draft"]},
            ]

        scanner = AssignedIssuesScanner(host=host, list_assigned=fake_list, ready_labels=("ready",))
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["x"]

    def test_no_user_no_signals(self) -> None:
        host = MagicMock(spec=CodeHostBackend)
        host.current_user.return_value = ""
        scanner = AssignedIssuesScanner(host=host, list_assigned=lambda *_: [], ready_labels=("ready",))
        assert scanner.scan() == []
