"""``resolve_diff_base`` — the ref every per-diff quality gate grades against.

Grading against the wrong base is what turns a fork's whole integration branch
into "new, uncovered" code and refuses an otherwise-clean PR. Each rung of the
chain — ``T3_DIFF_COVERAGE_BASE`` > ``teatree.targetBranch`` > the repo's real
default branch > ``origin/main`` — is exercised against a real git repo.
"""

from pathlib import Path

import pytest

from teatree.utils import git, git_branch, git_commit
from teatree.utils.git_branch import DIFF_BASE_CONFIG_KEY, DIFF_BASE_ENV, resolve_diff_base
from teatree.utils.git_run import run_strict


def _git(repo: Path, *args: str) -> str:
    return run_strict(repo=str(repo), args=list(args))


def _repo(tmp_path: Path, name: str = "clone", *, default_branch: str = "main", target_branch: str = "") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", default_branch)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default_branch}")
    if target_branch:
        _git(repo, "config", DIFF_BASE_CONFIG_KEY, target_branch)
    return repo


@pytest.fixture(autouse=True)
def _without_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DIFF_BASE_ENV, raising=False)


class TestResolutionOrder:
    def test_env_var_wins_over_the_git_config_target_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, target_branch="chore/integration")
        monkeypatch.setenv(DIFF_BASE_ENV, "develop")

        assert resolve_diff_base(str(repo)) == "origin/develop"

    def test_git_config_target_branch_wins_over_the_default_branch(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, default_branch="main", target_branch="chore/fork-integration")

        assert resolve_diff_base(str(repo)) == "origin/chore/fork-integration"

    def test_absent_git_config_falls_through_to_the_repos_default_branch(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, default_branch="master")

        assert resolve_diff_base(str(repo)) == "origin/master"

    def test_falls_back_to_origin_main_when_the_default_branch_is_unresolvable(self, tmp_path: Path) -> None:
        repo = tmp_path / "no-remote"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")

        assert resolve_diff_base(str(repo)) == "origin/main"


class TestRefQualification:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("develop", "origin/develop"),
            ("chore/integration-branch", "origin/chore/integration-branch"),
            ("origin/release", "origin/release"),
            ("refs/heads/release", "refs/heads/release"),
        ],
    )
    def test_env_configured_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str, expected: str
    ) -> None:
        repo = _repo(tmp_path)
        monkeypatch.setenv(DIFF_BASE_ENV, configured)

        assert resolve_diff_base(str(repo)) == expected

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("develop", "origin/develop"),
            ("chore/integration-branch", "origin/chore/integration-branch"),
            ("origin/release", "origin/release"),
            ("refs/heads/release", "refs/heads/release"),
        ],
    )
    def test_git_config_target_branch(self, tmp_path: Path, configured: str, expected: str) -> None:
        repo = _repo(tmp_path, target_branch=configured)

        assert resolve_diff_base(str(repo)) == expected

    def test_a_raw_sha_from_git_config_passes_through_untouched(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "config", DIFF_BASE_CONFIG_KEY, sha)

        assert resolve_diff_base(str(repo)) == sha

    def test_a_raw_sha_from_the_env_var_passes_through_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path)
        sha = _git(repo, "rev-parse", "HEAD")
        monkeypatch.setenv(DIFF_BASE_ENV, sha)

        assert resolve_diff_base(str(repo)) == sha


def test_the_resolver_is_defined_once_and_re_exported() -> None:
    """Two byte-identical copies mean a fix to one silently misses the other."""
    assert git.resolve_diff_base is git_branch.resolve_diff_base
    assert not hasattr(git_commit, "resolve_diff_base")
