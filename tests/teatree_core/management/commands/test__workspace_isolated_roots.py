"""``reap_orphan_isolated_worktree_roots`` — clean-all reaping of dead env dirs.

A git worktree's auto-isolated env dir (``~/.local/share/teatree-worktrees/
<slug>``, holding ``db.sqlite3`` + ``logs/``) lingers after the checkout is
gone, so clean-all reaps the dirs no live ``Worktree`` row references — but
never one that still holds a git checkout or any uncommitted/unpushed work
(#291, mirroring the #706/#835 data-loss discipline).
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree import paths
from teatree.core.management.commands._workspace import isolated_roots as reaper
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.utils.run import CommandFailedError
from tests._git_repo import make_git_repo, run_git

_REAP = "teatree.core.management.commands._workspace.isolated_roots"
_REGISTRY = "teatree.core.management.commands._workspace.checkout_registry"


def _make_env_dir(root: Path, slug: str) -> Path:
    """A realistic auto-isolated env dir: a per-worktree sqlite DB plus logs."""
    env_dir = root / slug
    (env_dir / "logs").mkdir(parents=True)
    (env_dir / "db.sqlite3").write_bytes(b"")
    return env_dir


class TestReapOrphanIsolatedWorktreeRoots(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        # Pin the checkout scan to the tmp workspace: unpinned it walks the real
        # home directory, which is both slow and host-dependent.
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))

    def _reap(self, *, dry_run: bool = False) -> list[str]:
        return reaper.reap_orphan_isolated_worktree_roots(self.workspace, dry_run=dry_run)

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

        result = self._reap()

        assert not orphan.exists()
        assert any("Removed orphan isolated worktree root" in line and orphan.name in line for line in result)

    def test_referenced_dir_is_kept(self) -> None:
        checkout = Path("/live/org/repo")
        self._make_worktree(checkout=checkout)
        referenced = _make_env_dir(self.root, paths.isolated_slug(checkout))

        result = self._reap()

        assert referenced.exists()
        assert not any("Removed orphan isolated worktree root" in line for line in result)

    def test_dir_holding_a_git_checkout_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/with/git"))
        env_dir = make_git_repo(self.root / slug, initial_commit=False)

        result = self._reap()

        assert env_dir.exists()
        assert any("KEPT" in line and slug in line for line in result)

    def test_dir_with_a_git_file_worktree_pointer_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/linked/wt"))
        env_dir = _make_env_dir(self.root, slug)
        (env_dir / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")

        result = self._reap()

        assert env_dir.exists()
        assert any("KEPT" in line and slug in line for line in result)

    def test_clean_ignored_slug_is_skipped(self) -> None:
        slug = paths.isolated_slug(Path("/gone/ignored"))
        env_dir = _make_env_dir(self.root, slug)
        with patch(f"{_REAP}.is_clean_ignored", return_value=True):
            result = self._reap()

        assert env_dir.exists()
        assert any("KEPT" in line and slug in line for line in result)

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

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: a busy pathless worktree's env dir was reaped"
        assert any("KEPT" in line and "live work" in line for line in result)

    def test_dead_pathless_row_still_reaps_orphan_dirs(self) -> None:
        """A pathless row whose ticket has NO live work does not protect an orphan dir.

        Preserves the safe-reap path: only LIVE work blocks reaping. A genuinely
        idle pathless row (no live session, no active/claimed task) cannot be
        mapped to a dir, so the unmatchable orphan is reaped as before.
        """
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291c")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="org/repo", branch="idle-no-path", extra={})
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = self._reap()

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

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: a worktree with an active task lost its env dir"
        assert any("KEPT" in line and "live work" in line for line in result)

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

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: a worktree under external delivery lost its env dir"
        assert any("KEPT" in line and "live work" in line for line in result)

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

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: a worktree with a recent E2E run lost its env dir"
        assert any("KEPT" in line and "live work" in line for line in result)

    def test_reaper_pinned_pathless_row_keeps_orphan_dirs(self) -> None:
        """A pathless row explicitly pinned protects env dirs (widened predicate, #2227)."""
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/291g")
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="org/repo", branch="pinned-no-path", extra={"reaper_pinned": True}
        )
        orphan = _make_env_dir(self.root, paths.isolated_slug(Path("/gone/elsewhere")))

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: an explicitly-pinned worktree lost its env dir"
        assert any("KEPT" in line and "live work" in line for line in result)

    def test_a_pinned_live_rows_env_dir_is_kept_even_though_its_checkout_is_gone(self) -> None:
        """Liveness is asked for a path-carrying row too, not only a pathless one.

        The widened keep-set must not become a shortcut past the operator pin: a
        row `worktree_protects_against_reap` protects keeps its isolated control
        DB whether or not any evidence source can still find its checkout on disk.
        """
        checkout = Path("/gone/but/pinned")
        worktree = self._make_worktree(checkout=checkout, branch="pinned-with-path")
        worktree.extra = {**worktree.extra, "reaper_pinned": True}
        worktree.save(update_fields=["extra"])
        env_dir = _make_env_dir(self.root, paths.isolated_slug(checkout))

        result = self._reap()

        assert env_dir.exists(), "DATA LOSS: an operator-pinned worktree lost its isolated control DB"
        assert any("KEPT" in line and env_dir.name in line for line in result)

    def test_missing_root_returns_empty(self) -> None:
        shutil.rmtree(self.root)
        assert self._reap() == []

    def test_dry_run_reports_a_reason_for_every_dir_it_keeps(self) -> None:
        checkout = Path("/live/org/repo")
        self._make_worktree(checkout=checkout)
        kept = paths.isolated_slug(checkout)
        _make_env_dir(self.root, kept)
        wiped = paths.isolated_slug(Path("/gone/org/repo"))
        _make_env_dir(self.root, wiped)

        result = self._reap(dry_run=True)

        assert any("KEPT" in line and kept in line and "live checkout" in line for line in result)
        assert any("WOULD" in line and wiped in line for line in result)

    def test_loose_files_in_root_are_ignored(self) -> None:
        (self.root / ".seed.lock").write_bytes(b"")

        result = self._reap()

        assert (self.root / ".seed.lock").exists()
        assert result == []


class TestLiveCheckoutEvidence(TestCase):
    """Git evidence joins DB rows in the keep-set, so the reaper's population matches the resolver's (#3852).

    ``paths.resolve_data_dir`` mints an env dir for ANY worktree checkout; the
    reaper asked only ``Worktree`` rows. On the host that produced this ticket
    that was 169 dirs against 13 rows, so 79 dirs owned by live-but-unregistered
    checkouts were reported as orphans — deleting them takes the isolated control
    DB out from under a live agent.
    """

    @staticmethod
    def _make_env_dir(root: Path, slug: str) -> Path:
        return _make_env_dir(root, slug)

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))
        self.clone = make_git_repo(self.workspace / "org" / "repo")
        # The row exists so ``candidate_clones`` reaches the clone; it deliberately
        # does NOT reference the checkout under test, which is the unregistered case.
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3852")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch="registered",
            extra={"worktree_path": str(self.clone), "clone_path": str(self.clone)},
        )

    def _add_checkout(self, branch: str) -> Path:
        checkout = self.workspace / branch
        run_git(self.clone, "worktree", "add", "-q", "-b", branch, str(checkout))
        return checkout

    def _reap(self, *, dry_run: bool = False) -> list[str]:
        return reaper.reap_orphan_isolated_worktree_roots(self.workspace, dry_run=dry_run)

    def test_live_unregistered_checkout_keeps_its_env_dir(self) -> None:
        """THE keystone: a git worktree with no ``Worktree`` row still owns its isolated DB.

        RED on the DB-rows-only keep-set — the checkout exists on disk and is in
        the clone's git registry, but no row references it, so the reaper removed
        the control DB the live checkout is actively using.
        """
        checkout = self._add_checkout("live-unregistered")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(checkout))

        result = self._reap()

        assert env_dir.exists(), "DATA LOSS: a live unregistered checkout's isolated control DB was reaped"
        assert any("KEPT" in line and env_dir.name in line for line in result)

    def test_dry_run_does_not_propose_deleting_a_live_checkouts_env_dir(self) -> None:
        checkout = self._add_checkout("live-preview")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(checkout))

        result = self._reap(dry_run=True)

        assert not any("WOULD" in line and env_dir.name in line for line in result)

    def test_env_dir_of_a_removed_checkout_is_still_reaped(self) -> None:
        """Anti-vacuous control: the widened keep-set must not keep EVERYTHING.

        Without this, a reaper that simply stopped deleting would pass the
        keystone test above while reclaiming nothing.
        """
        checkout = self._add_checkout("since-removed")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(checkout))
        run_git(self.clone, "worktree", "remove", "--force", str(checkout))

        result = self._reap()

        assert not env_dir.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_unreadable_clone_registry_keeps_every_dir(self) -> None:
        """Fail CLOSED: incomplete git evidence can never authorise a deletion (#706 spirit)."""
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(Path("/gone/org/repo")))
        failure = CommandFailedError(["git", "worktree", "list"], 128, "", "fatal: bad object")
        with patch(f"{_REGISTRY}.raw_worktree_paths", side_effect=failure):
            result = self._reap()

        assert env_dir.exists(), "DATA LOSS: dirs were reaped on incomplete checkout evidence"
        assert any("KEPT" in line and "could not list" in line for line in result)

    def test_owner_stamp_proves_liveness_for_a_clone_git_cannot_reach(self) -> None:
        """The durable complement: a stamped env dir names its owner, so liveness is proven, not inferred.

        ``isolated_slug`` is a one-way hash, so an env dir whose checkout lives in
        a clone no ``Worktree`` row points at is invisible to both evidence
        sources. The stamp makes the mapping invertible.
        """
        unreachable = Path(self.enterContext(tempfile.TemporaryDirectory())) / "checkout"
        unreachable.mkdir()
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(unreachable))
        paths.IsolatedEnvDir(env_dir).stamp_owner(unreachable)

        result = self._reap()

        assert env_dir.exists()
        assert any("KEPT" in line and "owner stamp" in line for line in result)

    def test_a_dir_touched_after_the_keep_set_snapshot_is_never_reaped(self) -> None:
        """TOCTOU: the box provisions continuously, so a snapshot goes stale mid-pass.

        The keep-set is computed once and the dirs are then iterated; an env dir
        minted between the two is absent from that keep-set through no fault of
        its own, and a snapshot-then-delete loop would reap it WHILE LIVE. Any dir
        whose mtime is at or after the snapshot instant is outside the evidence
        and must be kept.
        """
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(Path("/gone/racer")))
        future = time.time() + 3600
        os.utime(env_dir, (future, future))

        result = self._reap()

        assert env_dir.exists(), "DATA LOSS: an env dir minted after the keep-set snapshot was reaped"
        assert any("KEPT" in line and "changed after the keep-set" in line for line in result)

    def test_a_dir_untouched_since_the_snapshot_is_still_reaped(self) -> None:
        """Anti-vacuous control: the freshness guard must not keep everything."""
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(Path("/gone/settled")))
        old = time.time() - 3600
        os.utime(env_dir, (old, old))

        result = self._reap()

        assert not env_dir.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_a_discovered_live_checkout_gets_its_env_dir_stamped(self) -> None:
        """The durable evidence must GROW, or the invertible mapping never arrives.

        Only 3 of 185 dirs on the host carried a stamp, so the structural
        protection covered almost nothing. Every pass now stamps the env dir of
        each checkout it discovered, making the mapping invertible for everything
        reachable rather than only for dirs minted after the stamp shipped.
        """
        checkout = self._add_checkout("stamp-me")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(checkout))

        self._reap()

        assert paths.IsolatedEnvDir(env_dir).owner == checkout

    def test_owner_stamp_naming_a_vanished_checkout_in_a_visible_root_does_not_protect(self) -> None:
        """Anti-vacuous control for the stamp: it proves liveness, it is not a blanket pin.

        Re-scoped for #3872, not relaxed: the vanished checkout sits inside a root
        this venue can see, so its absence is this venue's to judge and the dir is
        still reaped. Only an owner beyond this venue's filesystem becomes unknown.
        """
        vanished = self.workspace / "vanished"
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(vanished))
        paths.IsolatedEnvDir(env_dir).stamp_owner(vanished)

        result = self._reap()

        assert not env_dir.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_owner_stamp_naming_a_path_beyond_this_venue_keeps_the_dir(self) -> None:
        """The #3872 keystone: an owner this venue cannot see is missing evidence.

        The isolated-env root is shared into the container while the clones that
        own those dirs are not, so each venue reaps exactly what it cannot see.
        The env dir and the owner's surviving ancestor then sit on different
        filesystems, which is the venue-independent tell a scan result can never be.
        """
        unseen = Path("/elsewhere/teatree-deploy/.claude/worktrees/agent")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(unseen))
        paths.IsolatedEnvDir(env_dir).stamp_owner(unseen)
        env_device = env_dir.stat().st_dev

        def device_of(path: Path) -> int | None:
            return env_device + 1 if path == Path("/") else env_device

        with patch.object(paths, "_device_of", device_of):
            result = self._reap()

        assert env_dir.exists(), "an owner beyond this venue's view is never proof of death"
        assert any("cannot see" in line and env_dir.name in line for line in result)
