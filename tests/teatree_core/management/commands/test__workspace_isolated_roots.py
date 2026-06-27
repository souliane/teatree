"""``reap_orphan_isolated_worktree_roots`` — clean-all reaping of dead env dirs.

A git worktree's auto-isolated env dir (``~/.local/share/teatree-worktrees/
<slug>``, holding ``db.sqlite3`` + ``logs/``) lingers after the checkout is
gone, so clean-all reaps the dirs no live ``Worktree`` row references — but
never one that still holds a git checkout or any uncommitted/unpushed work
(#291, mirroring the #706/#835 data-loss discipline).
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree import paths
from teatree.core.management.commands import _workspace_isolated_roots as reaper
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery
from tests._git_repo import make_git_repo

_REAP = "teatree.core.management.commands._workspace_isolated_roots"


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
        env_dir = make_git_repo(self.root / slug, initial_commit=False)

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

    def test_busy_pathless_row_keeps_orphan_dirs(self) -> None:
        """A BUSY worktree whose row lost its checkout path protects every env dir (#291 data-loss).

        The data-loss bug this pins: a live worktree whose canonical row is
        missing ``worktree_path`` (the stale-row class the resolver tolerates)
        cannot be hashed to a slug, so its in-use isolated DB looks like an
        orphan and was reaped out from under the mid-task agent. With a live
        :class:`Session` on its ticket, no unreferenced dir can be proven dead,
        so the reaper must KEEP them all.

        This is the documented red-first inversion: the prior test asserted the
        pathless row's would-be dir is reaped — the wrong, data-losing behavior.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291b")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="busy-no-path", extra={})
        Session.objects.create(ticket=ticket, overlay="test")  # live: ended_at is null
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert orphan.exists(), "DATA LOSS: a busy pathless worktree's env dir was reaped"
        assert any("SKIPPED" in line and "live work" in line for line in result)

    def test_dead_pathless_row_still_reaps_orphan_dirs(self) -> None:
        """A pathless row whose ticket has NO live work does not protect an orphan dir.

        Preserves the safe-reap path: only LIVE work blocks reaping. A genuinely
        idle pathless row (no live session, no active/claimed task) cannot be
        mapped to a dir, so the unmatchable orphan is reaped as before.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291c")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="idle-no-path", extra={})
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert not orphan.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_busy_via_claimed_task_pathless_row_keeps_orphan_dirs(self) -> None:
        """A claimed-Task (no live session) on a pathless row also protects env dirs."""
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291d")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="task-no-path", extra={})
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
        Task.objects.create(ticket=ticket, session=session, status=Task.Status.CLAIMED)
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert orphan.exists(), "DATA LOSS: a worktree with an active task lost its env dir"
        assert any("SKIPPED" in line and "live work" in line for line in result)

    def test_external_delivery_pathless_row_keeps_orphan_dirs(self) -> None:
        """A pathless row under a live external-delivery lease protects env dirs (#2227).

        The widened predicate: the destructive isolated-root reaper must not
        protect LESS than the reversible idle-stack reaper, which honors the
        external-delivery lease.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291e")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="lease-no-path", extra={})
        mark_external_delivery(ticket)
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert orphan.exists(), "DATA LOSS: a worktree under external delivery lost its env dir"
        assert any("SKIPPED" in line and "live work" in line for line in result)

    def test_recent_e2e_pathless_row_keeps_orphan_dirs(self) -> None:
        """A pathless row with a recent E2E run protects env dirs (widened predicate, #2227)."""
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291f")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="e2e-no-path",
            extra={},
            last_e2e_run=timezone.now(),
        )
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert orphan.exists(), "DATA LOSS: a worktree with a recent E2E run lost its env dir"
        assert any("SKIPPED" in line and "live work" in line for line in result)

    def test_reaper_pinned_pathless_row_keeps_orphan_dirs(self) -> None:
        """A pathless row explicitly pinned protects env dirs (widened predicate, #2227)."""
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291g")
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="org/repo", branch="pinned-no-path", extra={"reaper_pinned": True}
        )
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert orphan.exists(), "DATA LOSS: an explicitly-pinned worktree lost its env dir"
        assert any("SKIPPED" in line and "live work" in line for line in result)

    def test_missing_root_returns_empty(self) -> None:
        shutil.rmtree(self.root)
        assert reaper.reap_orphan_isolated_worktree_roots() == []

    def test_loose_files_in_root_are_ignored(self) -> None:
        (self.root / ".seed.lock").write_bytes(b"")

        result = reaper.reap_orphan_isolated_worktree_roots()

        assert (self.root / ".seed.lock").exists()
        assert result == []
