"""Tests for review_request.py."""

import json
from unittest.mock import patch

import pytest
from review_request import _ci_display, _discover_and_validate, _validate_mr_description, _validate_mr_title, main


class TestValidateMrTitle:
    @pytest.mark.parametrize(
        "title",
        ["fix(scope): do something", "feat: add feature", "improvement(ui): better layout", "refactor: clean up"],
    )
    def test_valid_titles(self, title: str) -> None:
        assert _validate_mr_title(title) == []

    @pytest.mark.parametrize("title", ["Do something", "WIP: fix stuff", "FEAT: wrong case"])
    def test_invalid_titles(self, title: str) -> None:
        assert len(_validate_mr_title(title)) > 0


class TestValidateMrDescription:
    def test_valid(self) -> None:
        assert _validate_mr_description("fix(scope): desc\n\nBody") == []

    def test_empty(self) -> None:
        assert any("Empty" in i for i in _validate_mr_description(""))

    def test_empty_first_line(self) -> None:
        assert any("empty" in i.lower() for i in _validate_mr_description("\n\nBody only"))


class TestCiDisplay:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [("success", "green"), ("failed", "failed"), ("running", "running"), ("pending", "running"), (None, "unknown")],
    )
    def test_mapping(self, status: str | None, expected: str) -> None:
        assert _ci_display(status) == expected


class TestDiscoverAndValidate:
    def _make_mr(self, **overrides: str) -> dict:
        base = {
            "iid": 123,
            "title": "fix(scope): do something",
            "description": "fix(scope): do something\n\nBody",
            "web_url": "https://gitlab.com/org/repo/-/merge_requests/123",
            "source_branch": "ac-fix-123",
            "updated_at": "2026-03-17T10:00:00Z",
            "draft": False,
            "_repo_path": "org/repo",
            "_repo_short": "repo",
            "_project_id": 42,
        }
        base.update(overrides)
        return base

    def test_empty_when_no_mrs(self) -> None:
        with patch("review_request.discover_mrs", return_value=[]):
            assert _discover_and_validate(["org/repo"], "user") == []

    def test_returns_enriched_mr(self) -> None:
        with (
            patch("review_request.discover_mrs", return_value=[self._make_mr()]),
            patch("review_request.get_mr_pipeline", return_value={"status": "success", "url": "http://ci"}),
            patch("review_request.get_mr_approvals", return_value={"count": 0, "required": 1}),
        ):
            results = _discover_and_validate(["org/repo"], "user")
            assert len(results) == 1
            assert results[0]["valid"] is True
            assert results[0]["ci_status"] == "green"

    def test_verbose_output(self) -> None:
        with (
            patch("review_request.discover_mrs", return_value=[self._make_mr()]),
            patch("review_request.get_mr_pipeline", return_value={"status": "success", "url": None}),
            patch("review_request.get_mr_approvals", return_value={"count": 0, "required": 1}),
        ):
            results = _discover_and_validate(["org/repo"], "user", verbose=True)
            assert len(results) == 1


class TestMain:
    def test_exits_without_repos(self) -> None:
        with pytest.raises(SystemExit, match="1"):
            main(repos="", json_output=False, verbose=False)

    def test_exits_without_username(self) -> None:
        with patch("review_request.current_user", return_value=""), pytest.raises(SystemExit, match="1"):
            main(repos="org/repo", json_output=False, verbose=False)

    def test_no_mrs_found(self) -> None:
        with (
            patch("review_request.current_user", return_value="testuser"),
            patch("review_request._discover_and_validate", return_value=[]),
        ):
            main(repos="org/repo", json_output=False, verbose=False)

    def test_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        mr_data = [
            {
                "repo": "repo",
                "iid": 1,
                "title": "fix: test",
                "valid": True,
                "ci_status": "green",
                "validation_issues": [],
                "approvals": 0,
                "approvals_required": 1,
                "url": "http://x",
                "updated_at": "2026-01-01",
            }
        ]
        with (
            patch("review_request.current_user", return_value="testuser"),
            patch("review_request._discover_and_validate", return_value=mr_data),
        ):
            main(repos="org/repo", json_output=True, verbose=False)
        assert json.loads(capsys.readouterr().out)[0]["ci_status"] == "green"

    def test_table_output_with_ready_mrs(self) -> None:
        mr_data = [
            {
                "repo": "repo",
                "iid": 1,
                "title": "fix: test",
                "valid": True,
                "ci_status": "green",
                "validation_issues": [],
                "approvals": 0,
                "approvals_required": 1,
                "url": "http://x",
                "updated_at": "2026-01-01",
            }
        ]
        with (
            patch("review_request.current_user", return_value="testuser"),
            patch("review_request._discover_and_validate", return_value=mr_data),
        ):
            main(repos="org/repo", json_output=False, verbose=False)

    def test_table_output_with_invalid_mr(self) -> None:
        mr_data = [
            {
                "repo": "repo",
                "iid": 1,
                "title": "bad title",
                "valid": False,
                "ci_status": "failed",
                "validation_issues": ["bad format"],
                "approvals": 0,
                "approvals_required": 1,
                "url": "http://x",
                "updated_at": "2026-01-01",
            }
        ]
        with (
            patch("review_request.current_user", return_value="testuser"),
            patch("review_request._discover_and_validate", return_value=mr_data),
        ):
            main(repos="org/repo", json_output=False, verbose=False)
