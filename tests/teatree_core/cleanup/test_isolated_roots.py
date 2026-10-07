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
from teatree.core.cleanup import isolated_roots as reaper
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.utils.run import CommandFailedError
from tests._git_repo import make_git_repo, run_git

_REAP = "teatree.core.cleanup.isolated_roots"
_REGISTRY = "teatree.core.cleanup.checkout_registry"


def _make_env_dir(root: Path, slug: str) -> Path:
    """A realistic auto-isolated env dir: a per-worktree sqlite DB plus logs."""
    env_dir = root / slug
    (env_dir / "logs").mkdir(parents=True)
    (env_dir / "db.sqlite3").write_bytes(b"")
    return env_dir


def _make_orphan_env_dir(root: Path, owner: Path) -> Path:
    """A genuine orphan as one looks after #3872: born stamped, its owner since deleted.

    The owner's parent directory exists, so this venue can read the neighbourhood the
    checkout would live in and find it absent — the observation that turns "I found no
    owner" into "the owner is gone", and the only form in which a dir is reclaimable.
    """
    env_dir = _make_env_dir(root, paths.isolated_slug(owner))
    paths.IsolatedEnvDir(env_dir).stamp_owner(owner)
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
            state=Ticket.State.WORK_STARTED,
        )
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="org/repo",
            branch=branch,
            extra={"worktree_path": str(checkout)},
        )

    def test_orphan_dir_whose_stamped_owner_this_venue_can_see_is_removed(self) -> None:
        """THE must-reap property, re-scoped by #3872 rather than relaxed.

        The guarantee is unchanged — a genuinely dead env dir IS reclaimed — but its
        subject is now a dir whose stamp names a path that does not exist AND lies
        within a root this venue can see. "No row references it" was never the same
        claim: it is satisfied identically by a dir whose owner is dead and by one
        whose owner is merely on a filesystem this process cannot reach.
        """
        orphan = _make_orphan_env_dir(self.root, self.workspace / "vanished-checkout")

        result = self._reap()

        assert not orphan.exists()
        assert any("Removed orphan isolated worktree root" in line and orphan.name in line for line in result)

    def test_a_stamped_owner_this_venue_cannot_see_is_kept_with_the_gap_reported(self) -> None:
        """THE keystone: the container geometry, where every step is right and the premise is false.

        RED before #3872: `sha256(<clone>/.claude/worktrees/hook-python-django)[:12]`
        is `1b2e4f54981d`, exactly the dir the container's dry-run offered to remove —
        a live, git-registered worktree holding a 1.2 GB control DB. The container has
        the isolated-env root bind-mounted and the clone that owns those dirs not, so
        the owner's whole parent chain is absent rather than deleted.
        """
        unmounted = self.workspace / "teatree-deploy" / ".claude" / "worktrees" / "hook-python-django"
        env_dir = _make_orphan_env_dir(self.root, unmounted)

        result = self._reap()

        assert env_dir.exists(), "DATA LOSS: a live checkout's control DB was reaped from a venue that cannot see it"
        assert any("KEPT" in line and env_dir.name in line and "cannot see" in line for line in result)

    def test_an_unstamped_dir_is_kept_because_nothing_recorded_who_owns_it(self) -> None:
        """Stage 3: once stamping is universal, an unstamped dir is unknown, never dead.

        A scan result is venue-dependent and a stamp is not, so a dir carrying no stamp
        leaves the reaper with only the venue-dependent answer — which is exactly the
        answer that offered to delete a live session's control DB.
        """
        env_dir = _make_env_dir(self.root, paths.isolated_slug(self.workspace / "never-stamped"))

        result = self._reap()

        assert env_dir.exists()
        assert any("KEPT" in line and env_dir.name in line and "unstamped" in line for line in result)

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
        orphan = _make_orphan_env_dir(self.root, self.workspace / "gone-elsewhere")

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
        wiped = _make_orphan_env_dir(self.root, self.workspace / "gone-org-repo").name

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
        env_dir = _make_orphan_env_dir(self.root, checkout)  # stamped at birth, as #3872 requires
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
        env_dir = _make_orphan_env_dir(self.root, self.workspace / "gone-settled")
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

    def test_owner_stamp_naming_a_vanished_checkout_does_not_protect(self) -> None:
        """Anti-vacuous control for the stamp: it proves liveness, it is not a blanket pin."""
        env_dir = _make_orphan_env_dir(self.root, self.workspace / "gone-stamped")

        result = self._reap()

        assert not env_dir.exists()
        assert any("Removed orphan isolated worktree root" in line for line in result)

    def test_a_dry_run_writes_nothing_into_the_env_dirs_it_inspects(self) -> None:
        checkout = self._add_checkout("preview-only")
        env_dir = self._make_env_dir(self.root, paths.isolated_slug(checkout))

        self._reap(dry_run=True)

        assert sorted(entry.name for entry in env_dir.iterdir()) == ["db.sqlite3", "logs"], "a preview wrote a stamp"

    def test_an_exhausted_walk_budget_leaves_the_pass_unable_to_vouch_for_a_dir(self) -> None:
        unstamped = self._make_env_dir(self.root, paths.isolated_slug(self.workspace / "never-stamped"))

        result = reaper.reap_orphan_isolated_worktree_roots(self.workspace, deadline=time.monotonic() - 1)

        assert unstamped.exists()
        assert any("KEPT" in line and unstamped.name in line and "budget" in line for line in result), result

    def test_control_a_walk_with_time_left_reads_the_same_dir_as_merely_unstamped(self) -> None:
        unstamped = self._make_env_dir(self.root, paths.isolated_slug(self.workspace / "never-stamped"))

        result = reaper.reap_orphan_isolated_worktree_roots(self.workspace, deadline=time.monotonic() + 600)

        assert any("KEPT" in line and unstamped.name in line and "unstamped" in line for line in result), result


class TestEvidenceIsJudgedPerDir(TestCase):
    """A gap blinds only what it hid, and absence counts only where the stamp's place is seen (#4923).

    The checkout scan always walks home, where a handful of unlistable dirs never
    clear, so a global veto kept every env dir forever. Lifting it is only safe beside
    the location record: the container's own home reads a host-only checkout as absent.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))

    def _reap(self) -> list[str]:
        return reaper.reap_orphan_isolated_worktree_roots(self.workspace)

    def _unlistable_dir(self) -> Path:
        locked = self.workspace / "locked"
        locked.mkdir(mode=0o300)
        self.addCleanup(locked.chmod, 0o700)
        return locked

    def test_an_unrelated_gap_no_longer_vetoes_a_location_proven_orphan(self) -> None:
        self._unlistable_dir()
        orphan = _make_orphan_env_dir(self.root, self.workspace / "vanished")

        result = self._reap()

        assert not orphan.exists(), result
        assert any("Removed orphan isolated worktree root" in line and orphan.name in line for line in result)

    def test_the_gap_still_keeps_a_dir_its_stamp_cannot_speak_for(self) -> None:
        self._unlistable_dir()
        unstamped = _make_env_dir(self.root, paths.isolated_slug(self.workspace / "never-stamped"))

        result = self._reap()

        assert unstamped.exists()
        assert any("KEPT" in line and unstamped.name in line and "incomplete" in line for line in result)

    def test_a_stamp_predating_the_location_record_is_kept(self) -> None:
        owner = self.workspace / "host-only"
        legacy = _make_env_dir(self.root, paths.isolated_slug(owner))
        (legacy / paths.OWNER_STAMP_NAME).write_text(f"{owner}\n", encoding="utf-8")

        result = self._reap()

        assert legacy.exists(), "DATA LOSS: a legacy stamp cannot tell this venue's path from the host's"
        assert any("KEPT" in line and legacy.name in line and "location" in line for line in result)

    def test_an_owner_stamped_in_another_venue_is_kept(self) -> None:
        orphan = _make_orphan_env_dir(self.root, self.workspace / "host-only")
        (orphan / paths.OWNER_LOCATION_NAME).write_text("259:2:/srv/host-only\n", encoding="utf-8")

        result = self._reap()

        assert orphan.exists(), "DATA LOSS: absence in the container's home is no proof about the host's"
        assert any("KEPT" in line and orphan.name in line and "another venue" in line for line in result)


class TestEachDirIsJudgedOnItsOwn(TestCase):
    """One dir this pass cannot judge is kept and named; it neither ends the pass nor erases its audit (#4923)."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))

    def _reap(self) -> list[str]:
        return reaper.reap_orphan_isolated_worktree_roots(self.workspace)

    def _stamped_env_dir(self, name: str, owner: Path) -> Path:
        env_dir = _make_env_dir(self.root, name)
        paths.IsolatedEnvDir(env_dir).stamp_owner(owner)
        return env_dir

    def _assert_kept_between_two_released_dirs(self, unjudgeable: Path, before: Path, after: Path, *, why: str) -> None:
        result = self._reap()

        assert unjudgeable.exists(), "DATA LOSS: a dir the pass could not judge was reaped"
        assert not before.exists(), result
        assert not after.exists(), "the pass ended at the dir it could not judge"
        assert any("Removed" in line and before.name in line for line in result), (
            "the audit of the earlier release is gone"
        )
        assert any("KEPT" in line and unjudgeable.name in line and why in line for line in result), result

    def test_an_owner_behind_a_dir_this_process_cannot_search_is_kept_and_the_pass_goes_on(self) -> None:
        sealed = self.workspace / "sealed"
        (sealed / "checkout").mkdir(parents=True)
        before = self._stamped_env_dir("1-before", self.workspace / "vanished-a")
        unjudgeable = self._stamped_env_dir("2-sealed", sealed / "checkout")
        after = self._stamped_env_dir("3-after", self.workspace / "vanished-b")
        sealed.chmod(0o200)
        self.addCleanup(sealed.chmod, 0o700)

        self._assert_kept_between_two_released_dirs(unjudgeable, before, after, why="cannot prove this dir is orphan")

    def test_a_dir_this_process_cannot_list_is_kept_and_the_pass_goes_on(self) -> None:
        before = self._stamped_env_dir("1-before", self.workspace / "vanished-a")
        unjudgeable = self._stamped_env_dir("2-unlistable", self.workspace / "vanished-c")
        after = self._stamped_env_dir("3-after", self.workspace / "vanished-b")
        unjudgeable.chmod(0o300)
        self.addCleanup(unjudgeable.chmod, 0o700)

        self._assert_kept_between_two_released_dirs(unjudgeable, before, after, why="could not be judged")

    def test_a_stamp_that_is_not_text_keeps_its_dir_and_the_pass_goes_on(self) -> None:
        before = self._stamped_env_dir("1-before", self.workspace / "vanished-a")
        garbled = self._stamped_env_dir("2-garbled", self.workspace / "vanished-c")
        (garbled / paths.OWNER_STAMP_NAME).write_bytes(b"\xff\xfe not utf-8")
        after = self._stamped_env_dir("3-after", self.workspace / "vanished-b")

        self._assert_kept_between_two_released_dirs(garbled, before, after, why="unstamped")

    def test_a_checkout_back_at_its_path_while_the_dir_is_judged_keeps_its_env_dir(self) -> None:
        owner = self.workspace / "re-provisioned"
        env_dir = self._stamped_env_dir("1-reused", owner)

        def reprovisioned_during_the_slow_checks(_name: str) -> bool:
            owner.mkdir(exist_ok=True)
            return False

        with patch(f"{_REAP}.is_clean_ignored", side_effect=reprovisioned_during_the_slow_checks):
            result = self._reap()

        assert (env_dir / "db.sqlite3").exists(), "DATA LOSS: a re-provisioned checkout lost its freshly seeded DB"
        assert any("KEPT" in line and env_dir.name in line and "live checkout" in line for line in result), result


class TestAnUnreadableStampInALiveCheckoutsEnvDir(TestCase):
    """The discovered-owner backfill runs before the per-dir verdicts and must survive a garbled stamp too (#4923)."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))
        self.clone = make_git_repo(self.workspace / "org" / "repo")

    def test_the_pass_goes_on_and_releases_the_eligible_orphan(self) -> None:
        live = _make_env_dir(self.root, paths.isolated_slug(self.clone))
        (live / paths.OWNER_STAMP_NAME).write_bytes(b"\xff\xfe not utf-8")
        orphan = _make_orphan_env_dir(self.root, self.workspace / "vanished")

        result = reaper.reap_orphan_isolated_worktree_roots(self.workspace)

        assert live.exists(), "DATA LOSS: a live checkout's env dir was reaped"
        assert not orphan.exists(), result
        assert any("KEPT" in line and live.name in line for line in result)


class TestGuardedRelease(TestCase):
    """The delete keeps salvage, survives teatree's own locked dirs, and one failure ends nothing (#4923)."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(paths, "auto_isolated_worktrees_dir", return_value=self.root))
        self.enterContext(patch(f"{_REGISTRY}.checkout_scan_roots", return_value=(self.workspace,)))

    def _reap(self) -> list[str]:
        return reaper.reap_orphan_isolated_worktree_roots(self.workspace)

    def test_unshipped_work_salvage_survives_the_release(self) -> None:
        orphan = _make_orphan_env_dir(self.root, self.workspace / "gone")
        bundle = orphan / "unshipped-work" / "abc123" / "uncommitted.patch"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("diff --git a/x b/x\n", encoding="utf-8")

        result = self._reap()

        assert bundle.exists(), "DATA LOSS: a salvage bundle `workspace restore` reads was deleted"
        assert not (orphan / "db.sqlite3").exists()
        assert paths.IsolatedEnvDir(orphan).owner == self.workspace / "gone", "salvage stays attributable"
        assert any("unshipped-work" in line and orphan.name in line for line in result)

    def test_a_salvage_only_dir_is_released_once_and_then_left_alone(self) -> None:
        orphan = _make_orphan_env_dir(self.root, self.workspace / "gone")
        bundle = orphan / "unshipped-work" / "abc123" / "uncommitted.patch"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("diff --git a/x b/x\n", encoding="utf-8")

        first = self._reap()
        second = self._reap()

        assert any(line.startswith("Released") and orphan.name in line for line in first), first
        assert second, "the dir that still holds salvage vanished from the account"
        assert all(line.startswith("KEPT") for line in second), f"released again with nothing left to release: {second}"
        assert bundle.exists()

    def test_a_locked_handoff_store_does_not_abort_the_pass(self) -> None:
        orphan = _make_orphan_env_dir(self.root, self.workspace / "dispatched-from")
        store = orphan / "handoff"
        delivery = store / "tmpdelivery"
        delivery.mkdir(parents=True)
        (delivery / "handoff.json").write_text("{}", encoding="utf-8")
        (delivery / "handoff.json").chmod(0o400)
        delivery.chmod(0o500)
        store.chmod(0o300)
        sibling = _make_orphan_env_dir(self.root, self.workspace / "also-gone")

        result = self._reap()

        assert not orphan.exists(), result
        assert not sibling.exists(), result

    def test_one_failing_dir_does_not_stop_the_rest(self) -> None:
        first = _make_orphan_env_dir(self.root, self.workspace / "gone-a")
        second = _make_orphan_env_dir(self.root, self.workspace / "gone-b")
        failing, survivor = sorted((first, second))
        real_rmtree = shutil.rmtree

        def refuse_one(path: Path) -> None:
            if path.is_relative_to(failing):
                raise PermissionError(13, "Permission denied", str(path))
            real_rmtree(path)

        with patch(f"{_REAP}.shutil.rmtree", side_effect=refuse_one):
            result = self._reap()

        assert failing.exists()
        assert not survivor.exists(), "one undeletable dir used to abort every later dir"
        assert any("FAILED" in line and failing.name in line for line in result)
        assert paths.IsolatedEnvDir(failing).owner is not None, "a failed release keeps the dir judgeable"

    def test_a_symlinked_entry_is_never_followed(self) -> None:
        elsewhere = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = _make_orphan_env_dir(elsewhere, self.workspace / "gone-target")
        link = self.root / target.name
        link.symlink_to(target)

        result = self._reap()

        assert (target / "db.sqlite3").exists(), "the reaper deleted through a link it never minted"
        assert link.is_symlink()
        assert any("KEPT" in line and link.name in line and "symlink" in line for line in result)
