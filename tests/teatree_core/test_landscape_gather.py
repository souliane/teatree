"""Integration for the intake landscape survey gather (:mod:`teatree.core.landscape_gather`, #2541).

Drives :func:`run_landscape` against a real ``git worktree`` under ``tmp_path``
(so the local worktree gather is real, not mocked) with the code host patched to
inject a fake forge. Proves the gather composes the survey and renders the
structured report, and that a missing code host degrades to a local-only survey
with a warning rather than aborting intake.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.landscape_gather import _workspace_worktree_paths, run_landscape
from teatree.types import RawAPIDict
from tests._git_repo import make_git_repo, run_git

_FACTORY = "teatree.core.landscape_gather.code_host_from_overlay"
_OVERLAY = "teatree.core.landscape_gather.get_overlay"


class _FakeHost:
    def __init__(
        self,
        *,
        my_prs: list[RawAPIDict],
        issues: list[RawAPIDict],
        merged_prs: list[RawAPIDict] | None = None,
    ) -> None:
        self._prs = my_prs
        self._issues = issues
        self._merged_prs = merged_prs or []

    def current_user(self) -> str:
        return "me"

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self._prs

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self._merged_prs

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self._issues


class _FakeOverlay:
    def get_merge_candidate_repo_slugs(self) -> list[str]:
        return ["owner/repo"]


class TestWorkspaceWorktreePaths(TestCase):
    @pytest.fixture(autouse=True)
    def _ws(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()

    def test_enumerates_linked_worktrees_that_exist(self) -> None:
        main = make_git_repo(self.workspace / "main")
        linked = self.workspace / "linked"
        run_git(main, "worktree", "add", "-b", "feature", str(linked))

        paths = _workspace_worktree_paths(self.workspace)

        assert linked.resolve() in {p.resolve() for p in paths}

    def test_non_git_workspace_yields_empty(self) -> None:
        assert _workspace_worktree_paths(self.workspace / "not-a-repo") == []


class TestRunLandscape(TestCase):
    @pytest.fixture(autouse=True)
    def _ws(self, tmp_path: Path) -> None:
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        # A worktree with an uncommitted change → in-flight local work.
        self.repo = make_git_repo(self.workspace / "repo")
        (self.repo / "dirty.txt").write_text("wip", encoding="utf-8")

    def test_composes_survey_with_prs_worktrees_and_recommendations(self) -> None:
        host = _FakeHost(
            my_prs=[{"url": "https://forge/pr/3", "title": "WIP (#50)"}],
            issues=[
                {"url": "https://forge/issues/50", "title": "in flight"},
                {"url": "https://forge/issues/99", "title": "genuine"},
            ],
        )
        with patch(_FACTORY, return_value=host), patch(_OVERLAY, return_value=_FakeOverlay()):
            report = run_landscape(self.workspace)

        assert any(wt["in_flight"] for wt in report["worktrees"])
        assert report["open_prs"][0]["url"] == "https://forge/pr/3"
        actions = {r["issue_url"]: r["action"] for r in report["recommendations"]}
        assert actions["https://forge/issues/50"] == "merge"
        assert actions["https://forge/issues/99"] == "keep"

    def test_merged_pr_marks_referenced_issue_done_close(self) -> None:
        # The §1b resolved-but-open path: a MERGED PR names an open issue, so the
        # survey recommends CLOSE/done. Reachable ONLY because run_landscape wires
        # survey_merged_pr_issue_numbers into the survey — revert that wiring and
        # this drops back to KEEP (the M1 anti-vacuity proof).
        host = _FakeHost(
            my_prs=[],
            merged_prs=[{"url": "https://forge/pr/7", "title": "ship it (#42)"}],
            issues=[{"url": "https://forge/issues/42", "title": "already shipped"}],
        )
        with patch(_FACTORY, return_value=host), patch(_OVERLAY, return_value=_FakeOverlay()):
            report = run_landscape(self.workspace)

        verdict = {r["issue_url"]: (r["disposition"], r["action"]) for r in report["recommendations"]}
        assert verdict["https://forge/issues/42"] == ("done", "close")

    def test_missing_code_host_degrades_to_local_only_with_warning(self) -> None:
        with patch(_FACTORY, return_value=None):
            report = run_landscape(self.workspace)

        assert report["open_prs"] == []
        assert report["recommendations"] == []
        assert any("no code host configured" in w for w in report["warnings"])
        # Local worktree landscape is still surveyed.
        assert len(report["worktrees"]) >= 1
