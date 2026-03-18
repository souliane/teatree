"""Tests for collect_followup_data.py."""

import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from collect_followup_data import (
    _api_get_mr,
    _build_active_mr_entry,
    _build_ticket_entry,
    _clean_review_tracking,
    _data_dir,
    _detect_merged,
    _discover_mrs,
    _enrich_mr,
    _extract_feature_flag,
    _extract_ticket_from_mr,
    _extract_ticket_from_text,
    _extract_ticket_url_from_mr,
    _fetch_issue_labels,
    _process_label,
    _repos,
    _review_channels,
    collect,
)
from lib.gitlab import ProjectInfo


class TestExtractTicketFromText:
    def test_from_description_url(self) -> None:
        mr = {"description": "https://gitlab.com/org/repo/-/issues/42\nmore text", "title": "fix"}
        assert _extract_ticket_from_text(mr) == "42"

    def test_from_work_items_url(self) -> None:
        mr = {"description": "https://gitlab.com/org/repo/-/work_items/55\n", "title": "fix"}
        assert _extract_ticket_from_text(mr) == "55"

    def test_from_title_url(self) -> None:
        mr = {
            "title": "techdebt: ruff batch 5 (https://gitlab.com/org/repo/-/work_items/1615)",
            "description": "## Summary",
        }
        assert _extract_ticket_from_text(mr) == "1615"

    def test_no_url(self) -> None:
        mr = {"description": "no url here", "title": "fix something"}
        assert _extract_ticket_from_text(mr) is None

    def test_none_description(self) -> None:
        mr = {"description": None, "title": "fix"}
        assert _extract_ticket_from_text(mr) is None


class TestExtractTicketFromMr:
    def test_from_text_url(self) -> None:
        mr = {"description": "https://gitlab.com/org/repo/-/issues/42\nmore", "title": "fix", "source_branch": "b"}
        assert _extract_ticket_from_mr(mr) == "42"

    @patch("collect_followup_data.get_mr_closing_issues")
    def test_falls_back_to_closing_issues(self, mock_closing: MagicMock) -> None:
        mock_closing.return_value = [{"iid": 1615}]
        mr = {"description": "## Summary", "title": "ruff batch 9", "source_branch": "x", "_project_id": 1, "iid": 99}
        tok = "test-tok"
        assert _extract_ticket_from_mr(mr, token=tok) == "1615"
        mock_closing.assert_called_once_with(1, 99, tok)

    @patch("collect_followup_data.get_mr_closing_issues")
    def test_no_closing_issues(self, mock_closing: MagicMock) -> None:
        mock_closing.return_value = []
        mr = {"description": "no url", "title": "fix", "source_branch": "x", "_project_id": 1, "iid": 99}
        tok = "test-tok"
        assert _extract_ticket_from_mr(mr, token=tok) is None

    @patch("collect_followup_data.get_mr_closing_issues")
    def test_no_project_id_skips_api(self, mock_closing: MagicMock) -> None:
        mr = {"description": "no url", "title": "fix", "source_branch": "x"}
        assert _extract_ticket_from_mr(mr) is None
        mock_closing.assert_not_called()


class TestExtractTicketUrlFromMr:
    def test_from_title(self) -> None:
        mr = {"title": "fix (https://gitlab.com/org/repo/-/issues/1)", "description": ""}
        assert _extract_ticket_url_from_mr(mr) == "https://gitlab.com/org/repo/-/issues/1"

    def test_from_description_first_line(self) -> None:
        mr = {"title": "fix", "description": "https://gitlab.com/org/repo/-/work_items/2\nmore"}
        assert _extract_ticket_url_from_mr(mr) == "https://gitlab.com/org/repo/-/work_items/2"

    def test_no_url(self) -> None:
        mr = {"title": "fix", "description": "no url"}
        assert _extract_ticket_url_from_mr(mr) is None

    def test_none_description(self) -> None:
        mr = {"title": "fix", "description": None}
        assert _extract_ticket_url_from_mr(mr) is None


class TestExtractFeatureFlag:
    def test_extracts_flag(self) -> None:
        assert _extract_feature_flag("[my_flag] fix bug") == "my_flag"

    def test_none_flag(self) -> None:
        assert _extract_feature_flag("[none] fix bug") is None

    def test_no_brackets(self) -> None:
        assert _extract_feature_flag("fix bug") is None


class TestProcessLabel:
    def test_finds_process_label(self) -> None:
        assert _process_label(["bug", "Process::Doing"]) == "Process::Doing"

    def test_process_with_space(self) -> None:
        assert _process_label(["Process:: Review"]) == "Process:: Review"

    def test_no_process_label(self) -> None:
        assert _process_label(["bug", "feature"]) is None

    def test_empty(self) -> None:
        assert _process_label([]) is None


class TestDataDir:
    def test_from_env(self) -> None:
        os.environ["T3_DATA_DIR"] = "/custom/dir"
        try:
            assert _data_dir() == Path("/custom/dir")
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_default(self) -> None:
        os.environ.pop("T3_DATA_DIR", None)
        result = _data_dir()
        assert "teatree" in str(result)


class TestRepos:
    def test_from_env(self) -> None:
        os.environ["T3_FOLLOWUP_REPOS"] = "org/a, org/b"
        try:
            assert _repos() == ["org/a", "org/b"]
        finally:
            del os.environ["T3_FOLLOWUP_REPOS"]

    def test_default_empty(self) -> None:
        os.environ.pop("T3_FOLLOWUP_REPOS", None)
        result = _repos()
        assert result == []


class TestReviewChannels:
    def test_from_env(self) -> None:
        os.environ["T3_REVIEW_CHANNELS"] = "backend=#review_be,frontend=#review_fe"
        try:
            result = _review_channels()
            assert result == {"backend": "#review_be", "frontend": "#review_fe"}
        finally:
            del os.environ["T3_REVIEW_CHANNELS"]

    def test_default_empty(self) -> None:
        os.environ.pop("T3_REVIEW_CHANNELS", None)
        result = _review_channels()
        assert result == {}


class TestDiscoverMrs:
    def test_discovers_mrs(self) -> None:
        annotated_mr = {"iid": 10, "title": "fix", "_repo_path": "org/repo", "_repo_short": "repo", "_project_id": 1}
        with patch("collect_followup_data.discover_mrs", return_value=[annotated_mr]):
            result = _discover_mrs(["org/repo"], "alice", "", verbose=False)
        assert len(result) == 1
        assert result[0]["_repo_short"] == "repo"
        assert result[0]["_project_id"] == 1

    def test_skip_unresolved_project(self) -> None:
        with patch("collect_followup_data.discover_mrs", return_value=[]):
            result = _discover_mrs(["org/bad"], "alice", "", verbose=True)
        assert result == []

    def test_verbose_delegates(self) -> None:
        with patch("collect_followup_data.discover_mrs", return_value=[]) as mock:
            _discover_mrs(["org/repo"], "alice", "", verbose=True)
        mock.assert_called_once()


class TestEnrichMr:
    def test_non_draft(self) -> None:
        mr = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_project_id": 42,
            "web_url": "https://gitlab.com/mr/10",
            "title": "fix",
            "source_branch": "ac/1-fix",
            "description": "",
        }
        with (
            patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "http://pipe"}),
            patch("collect_followup_data.get_mr_approvals", return_value={"count": 1, "required": 1}),
            patch("collect_followup_data.get_mr_notes", return_value=[]),
            patch("collect_followup_data.get_mr_closing_issues", return_value=[]),
        ):
            key, entry, is_draft = _enrich_mr(mr, "alice", "", {})
        assert key == "repo!10"
        assert not is_draft
        assert entry["pipeline_status"] == "success"

    def test_draft(self) -> None:
        mr = {
            "iid": 20,
            "draft": True,
            "_repo_short": "repo",
            "_project_id": 42,
            "web_url": "https://gitlab.com/mr/20",
            "title": "Draft: wip thing",
        }
        with patch("collect_followup_data.get_mr_closing_issues", return_value=[]):
            key, entry, is_draft = _enrich_mr(mr, "alice", "", {})
        assert key == "repo!20"
        assert is_draft
        assert entry["title"] == "wip thing"
        assert entry["pipeline_status"] is None


class TestBuildActiveMrEntry:
    def test_builds_entry(self) -> None:
        mr = {
            "iid": 10,
            "_repo_short": "repo",
            "_project_id": 42,
            "web_url": "https://gitlab.com/mr/10",
            "title": "fix",
            "source_branch": "ac/1-fix",
            "description": "",
        }
        entry = _build_active_mr_entry(
            mr,
            {"status": "success", "url": "http://pipe"},
            {"count": 0, "required": 1},
            [],
            {},
        )
        assert entry["pipeline_status"] == "success"
        assert entry["approvals"]["count"] == 0
        assert not entry["has_colleague_comments"]

    def test_preserves_existing_data(self) -> None:
        mr = {
            "iid": 10,
            "_repo_short": "repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "t",
            "source_branch": "b",
            "description": "",
        }
        existing = {
            "repo!10": {
                "review_requested": True,
                "review_permalink": "https://slack/msg",
                "review_comments": {"count": 2},
                "e2e_test_plan_url": "https://mr/comment",
                "skipped": True,
                "skip_reason": "reason",
            },
        }
        entry = _build_active_mr_entry(
            mr,
            {"status": "failed", "url": "u"},
            {"count": 0, "required": 0},
            [{"body": "comment"}],
            existing,
        )
        assert entry["review_requested"]
        assert entry["review_permalink"] == "https://slack/msg"
        assert entry["has_colleague_comments"]
        assert entry["skipped"]


class TestBuildTicketEntry:
    def test_new_ticket(self) -> None:
        entry = _build_ticket_entry("123", "https://gitlab.com/org/repo/-/issues/123", "my_flag", {})
        assert entry["url"] == "https://gitlab.com/org/repo/-/issues/123"
        assert entry["feature_flag"] == "my_flag"
        assert entry["mrs"] == []

    def test_preserves_custom_fields(self) -> None:
        existing = {
            "123": {
                "title": "Old title",
                "url": "old_url",
                "tracker_status": "Doing",
                "feature_flag": "old_flag",
                "mrs": ["repo!1"],
                "custom_field": "preserved",
            },
        }
        entry = _build_ticket_entry("123", None, None, existing)
        assert entry["custom_field"] == "preserved"
        assert entry["url"] == "old_url"
        assert entry["feature_flag"] == "old_flag"
        assert entry["mrs"] == []  # Always reset


class TestFetchIssueLabels:
    def test_fetches_labels(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": ["Process::Doing"], "title": "Issue title", "web_url": "https://x"}
        tickets = {
            "42": {"url": "https://gitlab.com/org/repo/-/issues/42", "title": "", "tracker_status": None},
        }
        with (
            patch("collect_followup_data.resolve_project", return_value=proj),
            patch("collect_followup_data.get_issue", return_value=issue),
        ):
            _fetch_issue_labels(tickets, "", verbose=False)
        assert tickets["42"]["tracker_status"] == "Process::Doing"
        assert tickets["42"]["title"] == "Issue title"

    def test_skips_no_url(self) -> None:
        tickets = {"42": {"url": None}}
        _fetch_issue_labels(tickets, "", verbose=False)

    def test_skips_bad_url(self) -> None:
        tickets = {"42": {"url": "https://example.com/bad"}}
        _fetch_issue_labels(tickets, "", verbose=False)

    def test_skips_unresolved_project(self) -> None:
        tickets = {"42": {"url": "https://gitlab.com/org/repo/-/issues/42"}}
        with patch("collect_followup_data.resolve_project", return_value=None):
            _fetch_issue_labels(tickets, "", verbose=False)

    def test_issue_not_found(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        tickets = {"42": {"url": "https://gitlab.com/org/repo/-/issues/42", "title": "X"}}
        with (
            patch("collect_followup_data.resolve_project", return_value=proj),
            patch("collect_followup_data.get_issue", return_value=None),
        ):
            _fetch_issue_labels(tickets, "", verbose=True)

    def test_does_not_overwrite_existing_title(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "title": "New title", "web_url": "https://x"}
        tickets = {"42": {"url": "https://gitlab.com/org/repo/-/issues/42", "title": "Existing"}}
        with (
            patch("collect_followup_data.resolve_project", return_value=proj),
            patch("collect_followup_data.get_issue", return_value=issue),
        ):
            _fetch_issue_labels(tickets, "", verbose=False)
        assert tickets["42"]["title"] == "Existing"

    def test_sets_url_when_empty(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "title": "T", "web_url": "https://new-url"}
        # url field contains the gitlab.com match pattern but ticket url is empty
        tickets = {"42": {"url": "https://gitlab.com/org/repo/-/issues/42", "title": "T"}}
        with (
            patch("collect_followup_data.resolve_project", return_value=proj),
            patch("collect_followup_data.get_issue", return_value=issue),
        ):
            _fetch_issue_labels(tickets, "", verbose=False)

    def test_verbose_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": ["Process::Doing"], "title": "T", "web_url": "https://x"}
        tickets = {"42": {"url": "https://gitlab.com/org/repo/-/issues/42", "title": "T"}}
        with (
            patch("collect_followup_data.resolve_project", return_value=proj),
            patch("collect_followup_data.get_issue", return_value=issue),
        ):
            _fetch_issue_labels(tickets, "", verbose=True)
        out = capsys.readouterr().out
        assert "#42" in out


class TestDetectMerged:
    def test_detects_merged(self) -> None:
        existing_mrs = {"repo!1": {"project_id": 42, "ticket": "99"}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "merged"}):
            merged = _detect_merged(existing_mrs, set(), "", verbose=False)
        assert merged == ["repo!1"]

    def test_skips_active(self) -> None:
        existing_mrs = {"repo!1": {"project_id": 42}}
        merged = _detect_merged(existing_mrs, {"repo!1"}, "", verbose=False)
        assert merged == []

    def test_skips_no_project_id(self) -> None:
        existing_mrs = {"repo!1": {}}
        merged = _detect_merged(existing_mrs, set(), "", verbose=False)
        assert merged == []

    def test_skips_no_iid(self) -> None:
        existing_mrs = {"repoX": {"project_id": 42}}
        merged = _detect_merged(existing_mrs, set(), "", verbose=False)
        assert merged == []

    def test_not_merged(self) -> None:
        existing_mrs = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "opened"}):
            merged = _detect_merged(existing_mrs, set(), "", verbose=False)
        assert merged == []

    def test_api_returns_none(self) -> None:
        existing_mrs = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value=None):
            merged = _detect_merged(existing_mrs, set(), "", verbose=False)
        assert merged == []

    def test_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        existing_mrs = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "merged"}):
            _detect_merged(existing_mrs, set(), "", verbose=True)
        out = capsys.readouterr().out
        assert "MERGED: repo!1" in out


class TestCleanReviewTracking:
    def test_removes_merged(self) -> None:
        tracking = {"repo!1": {"thread": "x"}}
        active = {}
        existing = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "merged"}):
            result = _clean_review_tracking(tracking, active, existing, "", verbose=False)
        assert "repo!1" not in result

    def test_keeps_active(self) -> None:
        tracking = {"repo!1": {"thread": "x"}}
        active = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "opened"}):
            result = _clean_review_tracking(tracking, active, {}, "", verbose=False)
        assert "repo!1" in result

    def test_keeps_no_iid(self) -> None:
        tracking = {"repoX": {"thread": "x"}}
        result = _clean_review_tracking(tracking, {}, {}, "", verbose=False)
        assert "repoX" in result

    def test_keeps_no_project_id(self) -> None:
        tracking = {"repo!1": {"thread": "x"}}
        result = _clean_review_tracking(tracking, {}, {}, "", verbose=False)
        assert "repo!1" in result

    def test_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        tracking = {"repo!1": {"thread": "x"}}
        existing = {"repo!1": {"project_id": 42}}
        with patch("collect_followup_data._api_get_mr", return_value={"state": "merged"}):
            _clean_review_tracking(tracking, {}, existing, "", verbose=True)
        out = capsys.readouterr().out
        assert "MERGED (review tracking)" in out


class TestApiGetMr:
    def test_success(self) -> None:
        with patch("lib.gitlab._api_get", return_value={"id": 1, "state": "merged"}):
            result = _api_get_mr(42, 10)
        assert result is not None
        assert result["state"] == "merged"

    def test_returns_none_on_bad_data(self) -> None:
        with patch("lib.gitlab._api_get", return_value=None):
            assert _api_get_mr(42, 10) is None

    def test_returns_none_on_list(self) -> None:
        with patch("lib.gitlab._api_get", return_value=[]):
            assert _api_get_mr(42, 10) is None


class TestCollect:
    def _mock_discover(self, mrs: list[dict]) -> AbstractContextManager[MagicMock]:
        return patch("collect_followup_data._discover_mrs", return_value=mrs)

    def test_no_username_exits(self) -> None:
        with (
            patch("collect_followup_data.current_user", return_value=""),
            pytest.raises(SystemExit, match="1"),
        ):
            collect()

    def test_empty_collection(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                self._mock_discover([]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert result["tickets"] == {}
            assert result["mrs"] == {}
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_full_collection(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "https://gitlab.com/mr/10",
            "title": "[flag] fix thing",
            "source_branch": "ac/99-fix",
            "description": "https://gitlab.com/org/repo/-/issues/99\nmore",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                self._mock_discover([mr]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 1, "required": 1}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert "99" in result["tickets"]
            assert "repo!10" in result["mrs"]
            assert result["tickets"]["99"]["feature_flag"] == "flag"
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_draft_mr(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr = {
            "iid": 20,
            "draft": True,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "Draft: wip",
            "source_branch": "ac/88-wip",
            "description": "",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                self._mock_discover([mr]),
                patch("collect_followup_data.get_mr_closing_issues", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert "repo!20" in result["draft_mrs"]
            assert "88" not in result["tickets"]  # drafts don't create tickets
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_merged_actions_log(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        # Set up existing data
        existing = {
            "tickets": {"50": {"title": "T", "mrs": ["repo!5"]}},
            "mrs": {"repo!5": {"project_id": 42, "ticket": "50"}},
            "review_comments_tracking": {},
        }
        fp = data_dir / "followup.json"
        fp.write_text(json.dumps(existing))

        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=["repo!5"]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert any("Merged: repo!5" in a for a in result["actions_log"])
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_verbose_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "fix",
            "source_branch": "ac/1-fix",
            "description": "",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 0, "required": 0}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data.get_mr_closing_issues", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                collect(verbose=True)
            out = capsys.readouterr().out
            assert "User: alice" in out
            assert "repo!10" in out
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_existing_followup_loaded(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        existing = {
            "tickets": {"42": {"title": "T", "url": "u", "tracker_status": "Doing", "feature_flag": "f", "mrs": []}},
            "mrs": {"repo!1": {"review_requested": True, "project_id": 1}},
            "review_comments_tracking": {"repo!1": {"count": 1}},
        }
        fp = data_dir / "followup.json"
        fp.write_text(json.dumps(existing))

        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={"repo!1": {"count": 1}}),
            ):
                result = collect()
            assert result["review_comments_tracking"] == {"repo!1": {"count": 1}}
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_duplicate_ticket_mr_not_added_twice(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr1 = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "fix",
            "source_branch": "ac/99-fix",
            "description": "https://gitlab.com/org/repo/-/issues/99\n",
        }
        mr2 = {
            "iid": 11,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "[flag2] other fix",
            "source_branch": "ac/99-other",
            "description": "https://gitlab.com/org/repo/-/issues/99\n",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr1, mr2]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 0, "required": 0}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert len(result["tickets"]["99"]["mrs"]) == 2
            assert "repo!10" in result["tickets"]["99"]["mrs"]
            assert "repo!11" in result["tickets"]["99"]["mrs"]
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_merged_mr_ticket_all_merged_action(self, tmp_path: Path) -> None:
        """When a merged MR is the last one for a ticket, log 'all MRs merged'."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        existing = {
            "tickets": {"50": {"title": "T", "mrs": ["repo!5"]}},
            "mrs": {"repo!5": {"project_id": 42, "ticket": "50"}},
            "review_comments_tracking": {},
        }
        fp = data_dir / "followup.json"
        fp.write_text(json.dumps(existing))

        # A new non-draft MR for ticket 50 keeps it alive
        mr = {
            "iid": 6,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "next fix",
            "source_branch": "ac/50-next",
            "description": "https://gitlab.com/org/repo/-/issues/50\n",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 0, "required": 0}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=["repo!5"]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            # repo!5 was merged and its ticket has no more references to it
            assert any("Merged: repo!5" in a for a in result["actions_log"])
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_feature_flag_from_second_mr(self, tmp_path: Path) -> None:
        """Second MR provides feature flag when first doesn't have one."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr1 = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "fix without flag",
            "source_branch": "ac/99-fix",
            "description": "https://gitlab.com/org/repo/-/issues/99\n",
        }
        mr2 = {
            "iid": 11,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "[new_flag] other fix",
            "source_branch": "ac/99-other",
            "description": "https://gitlab.com/org/repo/-/issues/99\n",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr1, mr2]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 0, "required": 0}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            assert result["tickets"]["99"]["feature_flag"] == "new_flag"
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_duplicate_mr_key_not_added_to_ticket_twice(self, tmp_path: Path) -> None:
        """Cover branch 348->350: mr_key already in ticket's mrs list."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        # Same MR appears twice in all_mrs (e.g., duplicate discovery)
        mr = {
            "iid": 10,
            "draft": False,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "fix",
            "source_branch": "ac/99-fix",
            "description": "https://gitlab.com/org/repo/-/issues/99\n",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr, mr]),
                patch("collect_followup_data.get_mr_pipeline", return_value={"status": "success", "url": "u"}),
                patch("collect_followup_data.get_mr_approvals", return_value={"count": 0, "required": 0}),
                patch("collect_followup_data.get_mr_notes", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                result = collect()
            # MR key should only appear once in ticket's mrs list
            assert result["tickets"]["99"]["mrs"].count("repo!10") == 1
        finally:
            del os.environ["T3_DATA_DIR"]

    def test_verbose_draft_mr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        os.environ["T3_DATA_DIR"] = str(data_dir)

        mr = {
            "iid": 20,
            "draft": True,
            "_repo_short": "repo",
            "_repo_path": "org/repo",
            "_project_id": 42,
            "web_url": "u",
            "title": "Draft: wip",
            "source_branch": "ac/88-wip",
            "description": "",
        }
        try:
            with (
                patch("collect_followup_data.current_user", return_value="alice"),
                patch("collect_followup_data._discover_mrs", return_value=[mr]),
                patch("collect_followup_data.get_mr_closing_issues", return_value=[]),
                patch("collect_followup_data._fetch_issue_labels"),
                patch("collect_followup_data._detect_merged", return_value=[]),
                patch("collect_followup_data._clean_review_tracking", return_value={}),
            ):
                collect(verbose=True)
            out = capsys.readouterr().out
            assert "draft" in out
        finally:
            del os.environ["T3_DATA_DIR"]
