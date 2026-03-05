"""Tests for cancel_stale_pipelines.py."""

from unittest.mock import patch

import pytest
from cancel_stale_pipelines import main
from lib.gitlab import ProjectInfo


class TestMain:
    def test_no_branch_exits(self) -> None:
        with (
            patch("cancel_stale_pipelines.current_branch", return_value=""),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".")

    def test_no_project_exits(self) -> None:
        with (
            patch("cancel_stale_pipelines.current_branch", return_value="feat-123"),
            patch("cancel_stale_pipelines.resolve_project_from_remote", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            main(".")

    def test_cancelled_pipelines(self, capsys: pytest.CaptureFixture[str]) -> None:
        proj = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("cancel_stale_pipelines.current_branch", return_value="feat-123"),
            patch("cancel_stale_pipelines.resolve_project_from_remote", return_value=proj),
            patch("cancel_stale_pipelines.cancel_pipelines", return_value=[100, 200]),
        ):
            main(".")
        out = capsys.readouterr().out
        assert "Cancelled pipeline #100" in out
        assert "Cancelled pipeline #200" in out
        assert "Cancelled 2 pipeline(s)" in out

    def test_no_pipelines_to_cancel(self, capsys: pytest.CaptureFixture[str]) -> None:
        proj = ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("cancel_stale_pipelines.current_branch", return_value="feat-123"),
            patch("cancel_stale_pipelines.resolve_project_from_remote", return_value=proj),
            patch("cancel_stale_pipelines.cancel_pipelines", return_value=[]),
        ):
            main(".")
        out = capsys.readouterr().out
        assert "No running/pending pipelines" in out
