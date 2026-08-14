"""The single-branch gate attributes a push to the repo that actually holds the branch.

The gate resolves the target repo from the command's effective dir, falling back
to the ambient hook ``cwd``. That fallback is where it over-blocked: the Bash
tool's working directory PERSISTS across calls while the harness keeps reporting
the session's original dir, so a bare ``git push`` issued after an earlier ``cd``
into a different repo's worktree is attributed to the session repo. When the
session repo is a pinned one, a push belonging entirely to another repository was
denied as that repo's "second branch".

An over-block here is not cosmetic: it teaches every lane to reach for
``[single-branch-ok:]``, which destroys the rule the gate exists to enforce. So
both directions are pinned — the genuine second-branch push must still deny.
"""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hooks.scripts import single_branch_repo_guard as guard

if TYPE_CHECKING:
    from teatree.core.gates.single_branch_repo_guard import SingleBranchFinding

_PINNED_BRANCH = "chore/integration"
_ENTRIES = [f"pinned-org/pinned-repo={_PINNED_BRANCH}"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S607 — git resolves off PATH in every venue this runs


def _make_repo(root: Path, name: str, slug: str, *branches: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", _PINNED_BRANCH)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "remote", "add", "origin", f"git@example.com:{slug}.git")
    for branch in branches:
        _git(repo, "branch", branch)
    return repo


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A pinned repo and an unrelated one, with the gate's config forced on."""
    monkeypatch.setattr(guard, "_declared_entries", lambda: list(_ENTRIES))
    monkeypatch.setattr(guard, "_gate_enabled", lambda: True)
    return {
        "pinned": _make_repo(tmp_path, "pinned-repo", "pinned-org/pinned-repo", "a-second-branch"),
        "other": _make_repo(tmp_path, "other-repo", "other-org/other-repo", "8680-feature"),
    }


def _finding(command: str, cwd: Path) -> "tuple[SingleBranchFinding, str, str] | None":
    """Mirror the gate's own return type so a caller can unpack/subscript the result."""
    return guard._finding(guard._load_core(), {"tool_input": {"command": command}, "cwd": str(cwd)})


class TestDoesNotOverBlockAnotherReposPush:
    def test_push_of_another_repos_branch_while_cwd_reports_the_pinned_repo_is_allowed(
        self, repos: dict[str, Path]
    ) -> None:
        """The reported false positive: an unrelated repo's push refused as a second branch here."""
        assert _finding("git push -u origin 8680-feature", repos["pinned"]) is None

    def test_push_of_another_repos_branch_with_an_explicit_git_c_is_allowed(self, repos: dict[str, Path]) -> None:
        """Naming the repo explicitly must not be the only way through — but it must still work."""
        command = f"git -C {repos['other']} push -u origin 8680-feature"
        assert _finding(command, repos["pinned"]) is None

    def test_push_of_a_branch_no_repo_holds_is_allowed(self, repos: dict[str, Path]) -> None:
        """An unproven premise fails OPEN — a push of a nonexistent branch would fail anyway."""
        assert _finding("git push -u origin never-existed", repos["pinned"]) is None


class TestStillBlocksAGenuineSecondBranch:
    def test_pushing_a_real_second_branch_of_the_pinned_repo_still_denies(self, repos: dict[str, Path]) -> None:
        """The premise check must not become a hole: the branch IS local to the pinned repo."""
        resolved = _finding("git push -u origin a-second-branch", repos["pinned"])

        assert resolved is not None
        found, pinned, _repo = resolved
        assert found.surface == "push"
        assert found.target == "a-second-branch"
        assert pinned == _PINNED_BRANCH

    def test_pushing_a_real_second_branch_via_git_c_into_the_pinned_repo_still_denies(
        self, repos: dict[str, Path]
    ) -> None:
        """Redirecting INTO the pinned repo from elsewhere is the case -C exists to catch."""
        command = f"git -C {repos['pinned']} push -u origin a-second-branch"
        resolved = _finding(command, repos["other"])

        assert resolved is not None
        assert resolved[0].target == "a-second-branch"

    def test_creating_a_second_branch_is_still_denied_though_the_branch_cannot_exist_yet(
        self, repos: dict[str, Path]
    ) -> None:
        """The creation surfaces are exempt from the ref check by design — it would blank them."""
        resolved = _finding("git checkout -b brand-new-branch", repos["pinned"])

        assert resolved is not None
        assert resolved[0].target == "brand-new-branch"

    def test_pushing_the_pinned_branch_itself_is_allowed(self, repos: dict[str, Path]) -> None:
        assert _finding(f"git push -u origin {_PINNED_BRANCH}", repos["pinned"]) is None
