"""Tests for the loop scanners — pure-Python signal collectors."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.notion_view import NotionViewScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.scanners.slack_mentions import SlackMentionsScanner


@dataclass
class FakeCodeHost:
    """In-memory CodeHostBackend conforming to the protocol — no MagicMock(spec=)."""

    user: str = ""
    my_prs: list[RawAPIDict] = field(default_factory=list)
    review_requested_prs: list[RawAPIDict] = field(default_factory=list)
    assigned_issues: list[RawAPIDict] = field(default_factory=list)
    list_assigned_issues_calls: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        _ = author
        return self.my_prs

    def list_review_requested_prs(self, *, reviewer: str) -> list[RawAPIDict]:
        _ = reviewer
        return self.review_requested_prs

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        self.list_assigned_issues_calls.append(assignee)
        return self.assigned_issues

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}


@dataclass
class FakeMessaging:
    """In-memory MessagingBackend conforming to the protocol."""

    mentions: list[RawAPIDict] = field(default_factory=list)
    dms: list[RawAPIDict] = field(default_factory=list)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return self.mentions

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return self.dms

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


class TestMyPrsScanner:
    def test_returns_empty_when_user_unknown(self) -> None:
        host = FakeCodeHost(user="")
        assert MyPrsScanner(host=host).scan() == []

    def test_failed_pipeline_yields_action_needed_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "iid": 7,
                    "title": "Fix thing",
                    "web_url": "https://gitlab/x/-/merge_requests/7",
                    "head_pipeline": {"status": "failed"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]
        assert "Fix thing" in signals[0].summary

    def test_unresolved_notes_yields_draft_notes_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 8, "title": "WIP", "web_url": "x", "user_notes_count": 3}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.draft_notes"]

    def test_clean_pr_yields_open_signal(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 9, "title": "Done", "web_url": "x", "head_pipeline": {"status": "success"}}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]

    def test_github_status_check_rollup_used_for_pipeline_state(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "number": 11,
                    "title": "GH PR",
                    "html_url": "https://github.com/o/r/pull/11",
                    "status_check_rollup": {"state": "failure"},
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_mergeable_state_string_used_when_rollup_missing(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                {
                    "number": 12,
                    "title": "GH PR",
                    "html_url": "https://github.com/o/r/pull/12",
                    "mergeable_state": "error",
                }
            ],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.failed"]

    def test_pr_without_url_or_title_still_emits_open_signal(self) -> None:
        """`_str_field` returns '' when neither web_url nor html_url is a string."""
        host = FakeCodeHost(
            user="alice",
            my_prs=[{"iid": 0, "title": None, "web_url": None}],
        )
        signals = MyPrsScanner(host=host).scan()
        assert [s.kind for s in signals] == ["my_pr.open"]
        assert signals[0].payload["url"] == ""
        assert signals[0].payload["title"] == ""


class TestReviewerPrsScanner:
    def test_unreviewed_first_pass_emits_signal(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]

    def test_new_sha_after_review_emits_new_sha(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "newer"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        scanner.mark_reviewed(url="https://gitlab/x/-/merge_requests/3", sha="older")
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.new_sha"]

    def test_already_reviewed_emits_nothing(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "same"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        scanner.mark_reviewed(url="https://gitlab/x/-/merge_requests/3", sha="same")
        assert scanner.scan() == []

    def test_no_reviewer_returns_no_signals(self, tmp_path: Path) -> None:
        host = FakeCodeHost(user="")
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        assert scanner.scan() == []

    def test_pr_without_url_is_skipped(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        assert scanner.scan() == []

    def test_head_sha_from_nested_head_dict(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {"html_url": "https://github.com/o/r/pull/1", "head": {"sha": "deadbeef"}},
            ],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        signals = scanner.scan()
        assert signals[0].payload["head_sha"] == "deadbeef"

    def test_head_sha_from_diff_refs(self, tmp_path: Path) -> None:
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {"web_url": "https://gitlab/x/-/merge_requests/2", "diff_refs": {"head_sha": "feedface"}},
            ],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=tmp_path / "cache.json")
        signals = scanner.scan()
        assert signals[0].payload["head_sha"] == "feedface"

    def test_corrupt_cache_is_treated_as_empty(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.json"
        cache.write_text("not-json-at-all", encoding="utf-8")
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/3", "sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=cache)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]

    def test_non_dict_cache_treated_as_empty(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache.json"
        cache.write_text("[1, 2, 3]", encoding="utf-8")
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[{"web_url": "https://gitlab/x/-/merge_requests/4", "sha": "abc"}],
        )
        scanner = ReviewerPrsScanner(host=host, cache_path=cache)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]

    def test_head_sha_returns_empty_when_no_field_present(self) -> None:
        from teatree.loop.scanners.reviewer_prs import _head_sha  # noqa: PLC0415

        assert _head_sha({}) == ""
        assert _head_sha({"sha": 123}) == ""
        assert _head_sha({"head": {"sha": 99}}) == ""
        assert _head_sha({"diff_refs": {"head_sha": True}}) == ""


class TestSlackMentionsScanner:
    def test_emits_one_signal_per_event(self, tmp_path: Path) -> None:
        backend = FakeMessaging(
            mentions=[{"ts": "1.0", "text": "hey"}],
            dms=[{"ts": "2.0", "text": "DM"}],
        )
        signals = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan()
        kinds = sorted(s.kind for s in signals)
        assert kinds == ["slack.dm", "slack.mention"]

    def test_empty_when_no_events(self, tmp_path: Path) -> None:
        backend = FakeMessaging()
        assert SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan() == []

    def test_persists_cursor_after_emitting_events(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        backend = FakeMessaging(
            mentions=[{"ts": "5.0", "text": "first"}, {"ts": "9.0", "text": "second"}],
        )
        SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert "9.0" in cursor.read_text(encoding="utf-8")

    def test_event_ts_fallback_when_ts_absent(self, tmp_path: Path) -> None:
        backend = FakeMessaging(
            mentions=[{"event_ts": "11.0", "text": "without ts"}],
        )
        signals = SlackMentionsScanner(backend=backend, cursor_path=tmp_path / "cur.json").scan()
        assert signals[0].payload["ts"] == "11.0"

    def test_corrupt_cursor_file_treated_as_empty(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        cursor.write_text("not-json", encoding="utf-8")
        backend = FakeMessaging(mentions=[{"ts": "1.0", "text": "x"}])
        signals = SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert len(signals) == 1

    def test_cursor_with_non_string_values_filtered(self, tmp_path: Path) -> None:
        cursor = tmp_path / "cur.json"
        cursor.write_text(
            '{"mentions": "1.0", "dms": 99, "extra": null}',
            encoding="utf-8",
        )
        backend = FakeMessaging(mentions=[{"ts": "2.0", "text": "x"}])
        signals = SlackMentionsScanner(backend=backend, cursor_path=cursor).scan()
        assert len(signals) == 1

    def test_default_cursor_path_used_when_omitted(self) -> None:
        from teatree.loop.scanners.slack_mentions import _default_cursor_path  # noqa: PLC0415

        backend = FakeMessaging()
        scanner = SlackMentionsScanner(backend=backend)
        assert scanner.cursor_path == _default_cursor_path()


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
        host = FakeCodeHost(
            user="alice",
            assigned_issues=[
                {"web_url": "x", "title": "ready", "labels": ["ready"]},
                {"web_url": "y", "title": "draft", "labels": ["draft"]},
            ],
        )
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",))
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["x"]
        assert host.list_assigned_issues_calls == ["alice"]

    def test_no_user_no_signals(self) -> None:
        host = FakeCodeHost(user="")
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",))
        assert scanner.scan() == []
        assert host.list_assigned_issues_calls == []

    def test_no_ready_labels_emits_signal_for_every_issue(self) -> None:
        host = FakeCodeHost(
            user="alice",
            assigned_issues=[
                {"web_url": "x", "title": "first", "labels": ["draft"]},
                {"web_url": "y", "title": "second", "labels": []},
            ],
        )
        scanner = AssignedIssuesScanner(host=host, ready_labels=())
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["x", "y"]

    def test_issue_without_url_emits_signal_with_empty_url(self) -> None:
        host = FakeCodeHost(
            user="alice",
            assigned_issues=[{"title": "no-url", "labels": []}],
        )
        scanner = AssignedIssuesScanner(host=host, ready_labels=())
        signals = scanner.scan()
        assert signals[0].payload["url"] == ""

    def test_dict_label_objects_resolved_by_name(self) -> None:
        host = FakeCodeHost(
            user="alice",
            assigned_issues=[
                {"html_url": "z", "title": "third", "labels": [{"name": "ready"}, {"name": "P1"}]},
            ],
        )
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",))
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["z"]
        assert "ready" in signals[0].payload["labels"]

    def test_non_list_labels_treated_as_empty(self) -> None:
        host = FakeCodeHost(
            user="alice",
            assigned_issues=[{"web_url": "w", "title": "weird", "labels": "ready"}],
        )
        scanner = AssignedIssuesScanner(host=host, ready_labels=("ready",))
        assert scanner.scan() == []
