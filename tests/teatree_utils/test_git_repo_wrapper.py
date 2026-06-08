"""GitRepo OOP wrapper delegates each method to the module-level function."""

from typing import Any
from unittest.mock import patch

import pytest

from teatree.utils import git as git_mod
from teatree.utils.git import GitRepo


@pytest.fixture
def repo() -> GitRepo:
    return GitRepo("/some/repo")


class TestGitRepoDelegation:
    @pytest.mark.parametrize(
        ("method", "module_fn", "args", "expected_call"),
        [
            ("merge_base", "merge_base", (), ("/some/repo", "origin/main")),
            ("merge_base", "merge_base", ("origin/dev",), ("/some/repo", "origin/dev")),
            ("rev_count", "rev_count", (), ("/some/repo", "")),
            ("log_oneline", "log_oneline", (), ("/some/repo", "")),
            ("unsynced_commits", "unsynced_commits", ("feat",), ("/some/repo", "feat", "origin/main")),
            ("status_porcelain", "status_porcelain", (), ("/some/repo",)),
            ("soft_reset", "soft_reset", (), ("/some/repo", "")),
            ("commit", "commit", ("msg",), ("/some/repo", "msg")),
            ("rebase", "rebase", (), ("/some/repo", "")),
            ("worktree_remove", "worktree_remove", (), ("/some/repo", "")),
            ("branch_delete", "branch_delete", ("br",), ("/some/repo", "br")),
            ("pull_ff_only", "pull_ff_only", (), ("/some/repo",)),
            ("default_branch", "default_branch", (), ("/some/repo",)),
            ("branch_merged", "branch_merged", ("br",), ("/some/repo", "br", "origin/main")),
            ("current_branch", "current_branch", (), ("/some/repo",)),
            ("remote_url", "remote_url", (), ("/some/repo", "origin")),
            ("remote_slug", "remote_slug", (), ("/some/repo", "origin")),
            ("config_value", "config_value", ("user.name",), ("/some/repo", "user.name")),
            ("last_commit_message", "last_commit_message", (), ("/some/repo",)),
            ("commit_messages", "commit_messages", ("a..b",), ("/some/repo", "a..b")),
        ],
    )
    def test_simple_methods_pass_repo_path(
        self,
        repo: GitRepo,
        method: str,
        module_fn: str,
        args: tuple[Any, ...],
        expected_call: tuple[Any, ...],
    ) -> None:
        with patch.object(git_mod, module_fn) as mock:
            getattr(repo, method)(*args)
        mock.assert_called_once_with(*expected_call)

    def test_fetch_with_ref(self, repo: GitRepo) -> None:
        with patch.object(git_mod, "fetch") as mock:
            repo.fetch(remote="upstream", ref="main")
        mock.assert_called_once_with("/some/repo", "upstream", "main")

    def test_push_with_branch(self, repo: GitRepo) -> None:
        with patch.object(git_mod, "push") as mock:
            repo.push(remote="origin", branch="feat")
        mock.assert_called_once_with("/some/repo", "origin", "feat")

    def test_worktree_add_forwards_create_branch_kwarg(self, repo: GitRepo) -> None:
        with patch.object(git_mod, "worktree_add") as mock:
            repo.worktree_add("/dest", "feat", create_branch=False)
        mock.assert_called_once_with("/some/repo", "/dest", "feat", create_branch=False)


class TestDefaultBranchDetection:
    def test_fallback_to_main_when_symbolic_ref_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_calls: list[list[str]] = []

        def fake_run(*, repo: str, args: list[str]) -> str:
            run_calls.append(args)
            return ""

        def fake_check(*, repo: str, args: list[str]) -> bool:
            # show-ref --verify --quiet refs/remotes/origin/<name>
            return args[-1] == "refs/remotes/origin/main"

        monkeypatch.setattr(git_mod, "run", fake_run)
        monkeypatch.setattr(git_mod, "check", fake_check)
        assert git_mod.default_branch("/r") == "main"

    def test_raises_when_no_default_branch_detectable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "run", lambda **_: "")
        monkeypatch.setattr(git_mod, "check", lambda **_: False)
        with pytest.raises(RuntimeError, match="Could not detect default branch"):
            git_mod.default_branch("/r")


class TestPush:
    def test_push_includes_branch_in_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[list[str]] = []

        def fake_run_strict(*, repo: str, args: list[str]) -> str:
            recorded.append(args)
            return ""

        monkeypatch.setattr(git_mod, "run_strict", fake_run_strict)
        git_mod.push("/r", remote="origin", branch="feat")
        assert recorded == [["push", "--set-upstream", "origin", "feat"]]

    def test_push_without_branch_only_sets_upstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[list[str]] = []
        monkeypatch.setattr(
            git_mod,
            "run_strict",
            lambda *, repo, args: recorded.append(args) or "",
        )
        git_mod.push("/r")
        assert recorded == [["push", "--set-upstream", "origin"]]


class TestBranchMerged:
    def test_matches_branch_in_merged_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "run", lambda **_: "  main\n  feat\n* dev")
        assert git_mod.branch_merged("/r", "feat") is True

    def test_returns_false_when_branch_not_in_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "run", lambda **_: "  main\n  other")
        assert git_mod.branch_merged("/r", "feat") is False


class TestRemoteSlug:
    def test_returns_slug_for_ssh_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "remote_url", lambda **_: "git@github.com:org/repo.git")
        assert git_mod.remote_slug(repo="/r") == "org/repo"

    def test_returns_slug_for_https_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "remote_url", lambda **_: "https://gitlab.com/org/repo.git")
        assert git_mod.remote_slug(repo="/r") == "org/repo"

    def test_returns_empty_when_remote_url_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "remote_url", lambda **_: "")
        assert git_mod.remote_slug(repo="/r") == ""

    def test_passes_through_when_repo_already_slug_shaped(self) -> None:
        assert git_mod.remote_slug(repo="org/repo") == "org/repo"

    def test_returns_empty_for_unparseable_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(git_mod, "remote_url", lambda **_: "noscheme")
        assert git_mod.remote_slug(repo="/r") == ""
