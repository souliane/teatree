"""``reap_orphan_isolated_worktree_roots`` — clean-all reaping of dead env dirs.

A git worktree's auto-isolated env dir (``~/.local/share/teatree-worktrees/
<slug>``, holding ``db.sqlite3`` + ``logs/``) lingers after the checkout is
gone, so clean-all reaps the dirs no live ``Worktree`` row references — but
never one that still holds a git checkout or any uncommitted/unpushed work
(#291, mirroring the #706/#835 data-loss discipline).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree import paths
from teatree.core.management.commands import _workspace_isolated_roots as reaper
from teatree.core.models import Ticket, Worktree

_REAP = "teatree.core.management.commands._workspace_isolated_roots"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)  # noqa: S607


def _make_env_dir(root: Path, slug: str) -> Path:
    """A realistic auto-isolated env dir: a per-worktree sqlite DB plus logs."""
    env_dir = root / slug
    (env_dir / "logs").mkdir(parents=True)
    (env_dir / "db.sqlite3").write_bytes(b"")
    return env_dir


class TestReapOrphanIsolatedWorktreeRoots(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))

    def _make_worktree(self, *, checkout: Path, branch: str = "fix-291") -> Worktree:
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/291",
            state=Ticket.State.STARTED,
        )
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch=branch,
            extra={"worktree_path": str(checkout)},
        )

    def test_orphan_dir_with_no_row_is_removed(self) -> None:
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/org/repo")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert not orphan.exists()
        assert any("Removed orphan isolated worktree root" in line and orphan.name in line for line in result)

    def test_referenced_dir_is_kept(self) -> None:
        checkout = Path("/live/org/repo")
        self._make_worktree(checkout=checkout)
        referenced = _make_env_dir(self.root, paths.isolated_slug(checkout))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert referenced.exists()
        assert not any("Removed orphan isolated worktree root" in line for line in result)

    def test_dir_holding_a_git_checkout_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/with/git"))
        env_dir = self.root / slug
        env_dir.mkdir()
        _git(env_dir, "init")

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert env_dir.exists()
        assert any("SKIPPED" in line and slug in line for line in result)

    def test_dir_with_a_git_file_worktree_pointer_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/linked/wt"))
        env_dir = _make_env_dir(self.root, slug)
        (env_dir / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert env_dir.exists()
        assert any("SKIPPED" in line and slug in line for line in result)

    def test_clean_ignored_slug_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/ignored"))
        env_dir = _make_env_dir(self.root, slug)
        with patch(f"{_REAP}.is_clean_ignored", return_value=True):
            result = reaper.reap_orphan_isolated_worktree_roots()

        assert env_dir.exists()
        assert any("SKIPPED" in line and slug in line for line in result)

    def test_row_without_checkout_path_does_not_protect_a_dir(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291b")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="no-path", extra={})
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert not orphan.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_missing_root_returns_empty(self) -> None:
        shutil.rmtree(self.root)
        assert reaper.reap_orphan_isolated_worktree_roots() == []

    def test_loose_files_in_root_are_ignored(self) -> None:
        (self.root / ".seed.lock").write_bytes(b"")

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert (self.root / ".seed.lock").exists()
        assert result == []
