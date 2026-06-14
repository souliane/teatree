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

from teatree.core.gates.owned_repo_guard import (
    PushScopeVerdict,
    UnownedRepoError,
    classify_push_for_overlays,
    merge_scope_verdict,
    require_owned_or_approved,
)
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


_ACME_PATH_ONLY = {"github.com": ["acme-eng"]}


class TestPathOnlyOwnedScope:
    """A path-only overlay's opted-in ``owned_repos`` is honored by the gate.

    A path-only TOML overlay cannot be instantiated, so it never appears in
    ``get_all_overlays()`` and its ``owned_repos`` is invisible to a gate that
    only iterates instantiable overlays. The push and merge classifiers accept
    the path-only opted-in scopes alongside the instantiable overlays so a
    repo owned by a path-only overlay is in scope (ALLOW) and one owned by
    neither is held (REQUIRE_APPROVAL). Before the fix the classifiers had no
    ``path_only_scopes`` parameter at all, so this is a TypeError RED.
    """

    def test_repo_owned_only_by_a_path_only_overlay_is_allowed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "acme", "git@github.com:acme-eng/widget.git")
        verdict = classify_push_for_overlays(repo, {}, path_only_scopes=[_ACME_PATH_ONLY])
        assert verdict is PushScopeVerdict.ALLOW

    def test_repo_owned_by_no_scope_including_path_only_requires_approval(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "other", "git@github.com:randomuser/randomrepo.git")
        verdict = classify_push_for_overlays(repo, {}, path_only_scopes=[_ACME_PATH_ONLY])
        assert verdict is PushScopeVerdict.REQUIRE_APPROVAL

    def test_no_opted_in_scope_at_all_allows(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "none", "git@github.com:randomuser/randomrepo.git")
        verdict = classify_push_for_overlays(repo, {}, path_only_scopes=[])
        assert verdict is PushScopeVerdict.ALLOW

    def test_merge_target_owned_by_path_only_overlay_is_allowed(self) -> None:
        verdict = merge_scope_verdict(
            "https://github.com/acme-eng/widget/pull/3",
            "acme-eng/widget",
            {},
            path_only_scopes=[_ACME_PATH_ONLY],
        )
        assert verdict is PushScopeVerdict.ALLOW

    def test_merge_target_owned_by_no_scope_requires_approval(self) -> None:
        verdict = merge_scope_verdict(
            "https://github.com/randomuser/randomrepo/pull/3",
            "randomuser/randomrepo",
            {},
            path_only_scopes=[_ACME_PATH_ONLY],
        )
        assert verdict is PushScopeVerdict.REQUIRE_APPROVAL
