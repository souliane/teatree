"""Tests for create_mr.py."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from create_mr import (
    _build_mr_description,
    _build_mr_title,
    _last_commit_message,
    _try_validate,
    main,
)
from lib.gitlab import ProjectInfo


class TestLastCommitMessage:
    def test_success(self) -> None:
        with patch("create_mr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="feat: add feature\n\nBody here")
            assert _last_commit_message("/repo") == "feat: add feature\n\nBody here"

    def test_failure(self) -> None:
        with patch("create_mr.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _last_commit_message("/repo") == ""


class TestBuildMrTitle:
    def test_with_gitlab_url_in_commit(self) -> None:
        line = "fix: resolve bug (https://gitlab.com/org/repo/-/issues/1)"
        assert _build_mr_title(line, "https://gitlab.com/org/repo/-/issues/2") == line

    def test_appends_ticket_url(self) -> None:
        result = _build_mr_title("fix: resolve bug", "https://gitlab.com/org/repo/-/issues/1")
        assert result == "fix: resolve bug (https://gitlab.com/org/repo/-/issues/1)"

    def test_no_ticket_url(self) -> None:
        assert _build_mr_title("fix: resolve bug", "") == "fix: resolve bug"


class TestBuildMrDescription:
    def test_with_body(self) -> None:
        desc = _build_mr_description("Title", "Some body text")
        assert "Title" in desc
        assert "Some body text" in desc
        assert "## Summary" not in desc

    def test_without_body(self) -> None:
        desc = _build_mr_description("Title", "")
        assert "## Summary" in desc

    def test_whitespace_only_body(self) -> None:
        desc = _build_mr_description("Title", "   ")
        assert "## Summary" in desc


class TestTryValidate:
    def test_valid(self) -> None:
        mock_result = SimpleNamespace(ok=True, errors=[], warnings=[])
        with patch.dict("sys.modules", {"lib.validate_mr": MagicMock(validate_mr=MagicMock(return_value=mock_result))}):
            assert _try_validate("title", "desc")

    def test_invalid(self) -> None:
        mock_result = SimpleNamespace(ok=False, errors=["bad title"], warnings=[])
        with patch.dict("sys.modules", {"lib.validate_mr": MagicMock(validate_mr=MagicMock(return_value=mock_result))}):
            assert not _try_validate("title", "desc")

    def test_with_warnings(self) -> None:
        mock_result = SimpleNamespace(ok=True, errors=[], warnings=["minor issue"])
        with patch.dict("sys.modules", {"lib.validate_mr": MagicMock(validate_mr=MagicMock(return_value=mock_result))}):
            assert _try_validate("title", "desc")

    def test_import_error(self) -> None:
        """When lib.validate_mr is not available, validation passes."""
        # Default behavior: no module in sys.modules, ImportError
        assert _try_validate("title", "desc")


class TestMain:
    def test_no_branch(self) -> None:
        with (
            patch("create_mr.current_branch", return_value=""),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".")

    def test_no_project(self) -> None:
        with (
            patch("create_mr.current_branch", return_value="feat-1"),
            patch("create_mr.resolve_project_from_remote", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".")

    def test_validation_failure(self) -> None:
        proj = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
        mock_result = SimpleNamespace(ok=False, errors=["bad"], warnings=[])
        with (
            patch("create_mr.current_branch", return_value="feat-1"),
            patch("create_mr.resolve_project_from_remote", return_value=proj),
            patch("create_mr.default_branch", return_value="master"),
            patch("create_mr.current_user", return_value="alice"),
            patch("create_mr.subprocess.run", return_value=MagicMock(returncode=0, stdout="feat: thing")),
            patch("create_mr.detect_ticket_dir", return_value=""),
            patch.dict("sys.modules", {"lib.validate_mr": MagicMock(validate_mr=MagicMock(return_value=mock_result))}),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".", dry_run=False, skip_validation=False)

    def test_create_success(self, capsys: pytest.CaptureFixture[str]) -> None:
        proj = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("create_mr.current_branch", return_value="feat-1"),
            patch("create_mr.resolve_project_from_remote", return_value=proj),
            patch("create_mr.default_branch", return_value="master"),
            patch("create_mr.current_user", return_value="alice"),
            patch("create_mr.subprocess.run", return_value=MagicMock(returncode=0, stdout="feat: thing\n\nBody text")),
            patch("create_mr.detect_ticket_dir", return_value=""),
            patch("create_mr.create_mr", return_value={"web_url": "https://gitlab.com/mr/1", "iid": 1}),
        ):
            main(".", dry_run=False, skip_validation=True)
        out = capsys.readouterr().out
        assert "Created !1" in out

    def test_create_failure(self) -> None:
        proj = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("create_mr.current_branch", return_value="feat-1"),
            patch("create_mr.resolve_project_from_remote", return_value=proj),
            patch("create_mr.default_branch", return_value="master"),
            patch("create_mr.current_user", return_value="alice"),
            patch("create_mr.subprocess.run", return_value=MagicMock(returncode=0, stdout="feat: thing")),
            patch("create_mr.detect_ticket_dir", return_value=""),
            patch("create_mr.create_mr", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".", dry_run=False, skip_validation=True)
