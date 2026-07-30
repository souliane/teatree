"""Real-git integration tests for checkout discovery.

The property under test is the one the container/host split broke: a worktree
must lead back to the clone whose git dir it commits through, even when that
clone is not the one teatree is installed from.
"""

from pathlib import Path

from teatree.core.gates.git_checkouts import _isolated_worktrees, discover_checkouts, owning_clone
from tests._git_repo import make_git_repo, run_git


class TestOwningClone:
    def test_worktree_resolves_to_its_clone_not_itself(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "host-checkout")
        worktree = tmp_path / "elsewhere" / "wt"
        worktree.parent.mkdir()
        run_git(clone, "worktree", "add", "-q", "-b", "feature", str(worktree))

        assert owning_clone(worktree) == clone

    def test_clone_resolves_to_itself(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "clone")

        assert owning_clone(clone) == clone

    def test_non_repo_resolves_to_none(self, tmp_path: Path) -> None:
        assert owning_clone(tmp_path) is None


class TestIsolatedWorktrees:
    def test_yields_untracked_checkouts_under_the_root(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "teatree-worktrees"
        clone = make_git_repo(tmp_path / "clone")
        worktree = root / "adhoc"
        root.mkdir()
        run_git(clone, "worktree", "add", "-q", "-b", "adhoc", str(worktree))
        (root / "not-a-checkout").mkdir()
        monkeypatch.setattr("teatree.core.gates.git_checkouts.auto_isolated_worktrees_dir", lambda: root)

        assert list(_isolated_worktrees()) == [worktree]

    def test_absent_root_yields_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "teatree.core.gates.git_checkouts.auto_isolated_worktrees_dir", lambda: tmp_path / "missing"
        )

        assert list(_isolated_worktrees()) == []


class TestDiscoverCheckouts:
    def test_reaches_the_clone_behind_an_untracked_worktree(self, tmp_path: Path, monkeypatch) -> None:
        """A worktree under the isolated root leads discovery back to its own clone."""
        installed = make_git_repo(tmp_path / "container-clone")
        host = make_git_repo(tmp_path / "host-checkout")
        root = tmp_path / "teatree-worktrees"
        root.mkdir()
        worktree = root / "wt"
        run_git(host, "worktree", "add", "-q", "-b", "feature", str(worktree))
        monkeypatch.setattr("teatree.core.gates.git_checkouts.auto_isolated_worktrees_dir", lambda: root)
        monkeypatch.setattr("teatree.core.gates.git_checkouts._installed_clone", lambda: installed)

        found = discover_checkouts()

        assert found.index(installed) < found.index(host) < found.index(worktree)

    def test_deduplicates_and_leads_with_the_installed_clone(self, tmp_path: Path, monkeypatch) -> None:
        installed = make_git_repo(tmp_path / "clone")
        root = tmp_path / "teatree-worktrees"
        root.mkdir()
        worktree = root / "wt"
        run_git(installed, "worktree", "add", "-q", "-b", "feature", str(worktree))
        monkeypatch.setattr("teatree.core.gates.git_checkouts.auto_isolated_worktrees_dir", lambda: root)
        monkeypatch.setattr("teatree.core.gates.git_checkouts._installed_clone", lambda: installed)

        assert discover_checkouts() == [installed, worktree]
