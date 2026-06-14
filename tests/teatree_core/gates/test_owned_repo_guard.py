"""Unknown-repo SCOPE gate (`owned_repo_guard`): opt-in, fail-CLOSED on unknown.

The gate holds an out-of-scope push/merge for the operator. It is opt-in
(``require_owned_repo_approval``), misconfig-guarded (empty ``owned_repos``
passes), satisfiable per-invocation (``approved``), and fails CLOSED on a
clean ``unknown`` verdict — the OPPOSITE polarity to the visibility gate.
"""

import os
import subprocess
from pathlib import Path

import pytest

from teatree.core.gates.owned_repo_guard import UnownedRepoError, require_owned_or_approved
from teatree.core.overlay import OverlayBase, OverlayConfig
from teatree.core.review_candidate import should_review_candidate


class _Overlay(OverlayBase):
    def __init__(self, *, owned: dict[str, list[str]], flag: bool) -> None:
        self.config = OverlayConfig()
        self.config.owned_repos = dict(owned)
        self.config.require_owned_repo_approval = flag

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: object) -> list[object]:  # type: ignore[override]
        _ = worktree
        return []


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


_TEATREE = {"github.com": ["souliane"]}


class TestFailsClosedOnUnknown:
    def test_unknown_repo_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "unk", "git@github.com:randomuser/randomrepo.git")
        overlay = _Overlay(owned=_TEATREE, flag=True)
        with pytest.raises(UnownedRepoError) as exc:
            require_owned_or_approved(repo, overlay)
        assert "randomuser/randomrepo" in str(exc.value)
        assert "owned_repos" in str(exc.value)

    def test_gitlab_repo_against_github_scope_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "gl", "git@gitlab.com:souliane/x.git")
        overlay = _Overlay(owned=_TEATREE, flag=True)
        with pytest.raises(UnownedRepoError):
            require_owned_or_approved(repo, overlay)


class TestPasses:
    def test_owned_repo_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "own", "git@github.com:souliane/teatree.git")
        require_owned_or_approved(repo, _Overlay(owned=_TEATREE, flag=True))

    def test_opt_in_off_passes_even_for_unknown(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "off", "git@github.com:randomuser/randomrepo.git")
        require_owned_or_approved(repo, _Overlay(owned=_TEATREE, flag=False))

    def test_empty_owned_repos_misconfig_guard_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "empty", "git@github.com:randomuser/randomrepo.git")
        require_owned_or_approved(repo, _Overlay(owned={}, flag=True))

    def test_per_invocation_approval_passes(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "appr", "git@github.com:randomuser/randomrepo.git")
        require_owned_or_approved(repo, _Overlay(owned=_TEATREE, flag=True), approved=True)


class TestOrthogonalToCollaboration:
    """SCOPE (owned) is orthogonal to COLLABORATION (self vs colleague).

    Owning a repo does NOT collapse into ``author_is_self`` — an owned repo
    whose MR a colleague authored is still a review candidate (routes to
    review, never auto-merges). The scope gate touches neither
    ``author_is_self`` nor ``can_auto_merge``.
    """

    def test_owned_repo_with_colleague_author_is_still_a_review_candidate(self) -> None:
        colleague_pr = {
            "user": {"login": "a-teammate"},
            "state": "open",
            "base": {"repo": {"full_name": "acme-eng/widget-overlay"}},
        }
        assert should_review_candidate(colleague_pr, current_user="souliane") is True

    def test_owned_repo_with_self_author_is_not_a_review_candidate(self) -> None:
        own_pr = {"user": {"login": "souliane"}, "state": "open"}
        assert should_review_candidate(own_pr, current_user="souliane") is False
