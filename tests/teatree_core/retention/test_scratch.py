"""Agent-scratch retention sweep — the guards, the report, and the fail-closed paths (#4165)."""

import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.ticket import Ticket
from teatree.core.models.worktree import Worktree
from teatree.core.retention import scratch
from teatree.core.retention.liveness import ProcessTableView
from teatree.core.retention.scratch import ScratchEntry, ScratchSweep, ScratchSweepPlan
from tests._procfs import answering_pid as _answering_pid
from tests._procfs import listening_socket as _listening_socket
from tests._procfs import net_unix as _net_unix
from tests._unreadable_file import skip_if_root


def _age(path: Path, *, days: float) -> None:
    stamp = timezone.now().timestamp() - days * 86400
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _scratch(root: Path, name: str, *, days: float, size: int = 16) -> Path:
    entry = root / name
    entry.write_bytes(b"x" * size)
    _age(entry, days=days)
    return entry


class ScratchSweepTestCase(TestCase):
    """Shared temp root + a synthetic process table the sweep reads instead of /proc."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.proc = Path(self.enterContext(TemporaryDirectory()))
        # A second, deliberately un-seeded table for the fail-closed tests: their
        # subject is a probe with no witness anywhere, which self.proc now has.
        self.blind_proc = Path(self.enterContext(TemporaryDirectory()))
        self.elsewhere = Path(self.enterContext(TemporaryDirectory()))
        _answering_pid(self.proc, self.elsewhere / "held-elsewhere")

    def sweep(
        self,
        *,
        retention_days: int = 3,
        proc_root: Path | None = None,
        probe_root: Path | None = None,
        uid: int | None = None,
        now: datetime | None = None,
    ) -> ScratchSweep:
        return ScratchSweep(
            root=self.root,
            retention_days=retention_days,
            probe_root=probe_root,
            proc_root=proc_root or self.proc,
            uid=uid,
            now=now,
        )

    def entry_for(self, plan: ScratchSweepPlan, path: Path) -> ScratchEntry:
        return next(item for item in plan.entries if item.path == str(path))


class ScratchSweepPlanTests(ScratchSweepTestCase):
    def test_stale_entry_is_removable_and_young_one_is_kept(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=7, size=4096)
        young = _scratch(self.root, "wt4081venv", days=1, size=8192)

        plan = self.sweep().plan()

        verdicts = {entry.path: entry.removable for entry in plan.entries}
        assert verdicts[str(stale)] is True
        assert verdicts[str(young)] is False
        assert plan.candidate_bytes == 4096

    def test_entries_are_size_ranked_so_the_report_leads_with_the_big_ones(self) -> None:
        # Names chosen so alphabetical order is NOT size order — otherwise the
        # directory listing alone would satisfy the assertion.
        _scratch(self.root, "a-small", days=7, size=10)
        _scratch(self.root, "b-huge", days=7, size=9000)
        _scratch(self.root, "c-medium", days=7, size=500)

        plan = self.sweep().plan()

        assert [Path(entry.path).name for entry in plan.entries] == ["b-huge", "c-medium", "a-small"]

    def test_an_entry_held_open_by_a_live_process_is_never_removable(self) -> None:
        held = _scratch(self.root, "costidx.sqlite3", days=9)
        (self.proc / "4242" / "fd").mkdir(parents=True)
        (self.proc / "4242" / "fd" / "3").symlink_to(held)

        entry = self.entry_for(self.sweep().plan(), held)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_a_process_cwd_inside_a_stale_dir_keeps_the_whole_dir(self) -> None:
        scratch_dir = self.root / "board3841"
        nested = scratch_dir / "nested"
        nested.mkdir(parents=True)
        _age(nested, days=9)
        _age(scratch_dir, days=9)
        (self.proc / "77").mkdir()
        (self.proc / "77" / "cwd").symlink_to(nested)

        entry = self.entry_for(self.sweep().plan(), scratch_dir)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_a_registered_worktree_checkout_is_never_removable(self) -> None:
        checkout = self.root / "rev3970"
        checkout.mkdir()
        _age(checkout, days=9)
        ticket = Ticket.objects.create(issue_url="https://example.test/issues/1", overlay="teatree")
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(checkout),
            branch="wip",
            extra={"worktree_path": str(checkout)},
        )

        entry = self.entry_for(self.sweep().plan(), checkout)

        assert entry.removable is False
        assert entry.reason == "holds a tracked worktree"

    def test_an_entry_nested_inside_a_registered_worktree_root_is_never_removable(self) -> None:
        nested = self.root / "checkouts"
        nested.mkdir()
        _age(nested, days=9)
        ticket = Ticket.objects.create(issue_url="https://example.test/issues/2", overlay="teatree")
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(nested / "repo"),
            branch="wip",
            extra={"worktree_path": str(nested / "repo" / "teatree")},
        )

        entry = self.entry_for(self.sweep().plan(), nested)

        assert entry.removable is False
        assert entry.reason == "holds a tracked worktree"

    def test_an_entry_holding_the_root_of_a_registered_worktree_is_never_removable(self) -> None:
        outer = Path(self.enterContext(TemporaryDirectory()))
        inner = ScratchSweep(root=outer, retention_days=3, proc_root=self.proc)
        child = outer / "wt"
        child.mkdir()
        _age(child, days=9)
        ticket = Ticket.objects.create(issue_url="https://example.test/issues/3", overlay="teatree")
        Worktree.objects.create(ticket=ticket, repo_path=str(outer), branch="wip", extra={})

        entry = next(item for item in inner.plan().entries if item.path == str(child))

        assert entry.removable is False
        assert entry.reason == "holds a tracked worktree"

    def test_protected_names_survive_any_age(self) -> None:
        protected = self.root / "claude-statusline"
        protected.mkdir()
        _age(protected, days=90)

        entry = self.entry_for(self.sweep().plan(), protected)

        assert entry.removable is False
        assert entry.reason == "protected path"

    def test_an_entry_owned_by_another_uid_is_never_removable(self) -> None:
        foreign = _scratch(self.root, "ho-probe", days=9)

        entry = self.entry_for(self.sweep(uid=os.getuid() + 1).plan(), foreign)

        assert entry.removable is False
        assert "not this one" in entry.reason

    def test_an_unreadable_process_table_removes_nothing_but_still_reports_the_sizes(self) -> None:
        _scratch(self.root, "t3after", days=9, size=2048)

        plan = self.sweep(proc_root=self.root / "no-such-proc").plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap
        assert plan.entries[0].size_bytes == 2048

    def test_retention_days_zero_disables_the_sweep(self) -> None:
        _scratch(self.root, "t3after", days=99)

        plan = self.sweep(retention_days=0).plan()

        assert plan.candidates == ()
        assert "retention disabled" in plan.probe_gap

    def test_probe_root_translates_container_paths_to_the_host_spelling(self) -> None:
        held = _scratch(self.root, "wt4081venv", days=9)
        (self.proc / "9" / "fd").mkdir(parents=True)
        # The host process table spells this file /tmp/<name> while the container
        # reaches it at <root>/<name>; without the translation the guard misses it.
        Path(self.proc / "9" / "fd" / "1").symlink_to(f"/tmp/{held.name}")

        entry = self.entry_for(self.sweep(probe_root=Path("/tmp")).plan(), held)

        assert entry.removable is False
        assert entry.reason == "open by a live process"


class ScratchSweepApplyTests(ScratchSweepTestCase):
    def test_apply_removes_the_stale_tree_and_reports_the_bytes(self) -> None:
        stale = self.root / "wt4081venv"
        stale.mkdir()
        lib = stale / "lib.so"
        lib.write_bytes(b"y" * 1024)
        _age(lib, days=8)
        _age(stale, days=8)
        keep = _scratch(self.root, "fresh", days=0.5, size=64)

        plan = self.sweep().apply()

        assert not stale.exists()
        assert keep.exists()
        assert plan.applied
        assert plan.reclaimed_bytes == 1024
        assert "reclaimed 0.00 GB" in plan.summary

    def test_apply_skips_an_entry_touched_between_the_plan_and_the_unlink(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=8, size=32)
        sweep = self.sweep()
        cleared = sweep.plan()
        assert [entry.removable for entry in cleared.entries] == [True]
        # The box provisions continuously: an agent touches the file after the plan
        # cleared it. The patch freezes that already-computed plan so the removal
        # loop is the only thing that can still catch the refresh.
        _age(stale, days=0)

        with patch.object(ScratchSweep, "plan", return_value=cleared):
            applied = sweep.apply()

        assert stale.exists()
        assert applied.reclaimed_bytes == 0

    def test_apply_removes_nothing_when_the_process_table_cannot_be_read(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=8)

        plan = self.sweep(proc_root=self.root / "absent").apply()

        assert stale.exists()
        assert plan.reclaimed_bytes == 0

    def test_a_stale_symlink_is_unlinked_without_following_it(self) -> None:
        target = Path(self.enterContext(TemporaryDirectory()))
        (target / "keep.txt").write_bytes(b"z")
        link = self.root / "dangling"
        link.symlink_to(target)
        _age(link, days=8)

        self.sweep(now=timezone.now() + timedelta(seconds=1)).apply()

        assert not link.is_symlink()
        assert (target / "keep.txt").exists()


class ScratchRootResolutionTests(ScratchSweepTestCase):
    """root + proc_root are resolved as a PAIR, so the guard never reads the wrong namespace."""

    def test_the_host_view_is_used_only_when_both_halves_are_mounted(self) -> None:
        host_tmp = Path(self.enterContext(TemporaryDirectory()))
        host_proc = Path(self.enterContext(TemporaryDirectory()))

        with (
            patch.object(scratch, "_HOST_TMP", host_tmp),
            patch.object(scratch, "_HOST_PROC", host_proc),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("TEATREE_HOST_TMP", None)
            paired = scratch.resolve_scratch_sweep()

        assert paired.root == host_tmp
        assert paired.proc_root == host_proc
        assert paired.probe_root == Path("/tmp"), "with no override this still names the plain default host /tmp"

    def test_the_host_views_probe_root_is_read_from_the_same_variable_the_mount_source_uses(self) -> None:
        """#4165 review finding: a hard-coded probe_root silently blinded the open-file guard.

        The compose mount source is ``${TEATREE_HOST_TMP:-/tmp}`` — when an operator
        overrides it, the guard's namespace must move with it or every open-file
        check compares against a path the host process table never spells.
        """
        host_tmp = Path(self.enterContext(TemporaryDirectory()))
        host_proc = Path(self.enterContext(TemporaryDirectory()))

        with (
            patch.object(scratch, "_HOST_TMP", host_tmp),
            patch.object(scratch, "_HOST_PROC", host_proc),
            patch.dict(os.environ, {"TEATREE_HOST_TMP": "/mnt/scratch"}),
        ):
            paired = scratch.resolve_scratch_sweep()

        assert paired.probe_root == Path("/mnt/scratch")

    def test_a_live_process_held_file_survives_under_a_custom_host_tmp_override(self) -> None:
        """The end-to-end regression for the review finding, not just the resolved value.

        Reverting the ``_HOST_TMP_ENV`` read (hard-coding ``probe_root=_VENUE_TMP``
        again) makes this go RED: the entry becomes removable because the guard
        compares against ``/tmp/agentdb.sqlite3`` while the process table (as read
        from the operator's real mount point) spells it ``/mnt/scratch/agentdb.sqlite3``.
        """
        host_tmp = Path(self.enterContext(TemporaryDirectory()))
        host_proc = Path(self.enterContext(TemporaryDirectory()))
        held = _scratch(host_tmp, "agentdb.sqlite3", days=9, size=64)
        (host_proc / "999" / "fd").mkdir(parents=True)
        (host_proc / "999" / "fd" / "3").symlink_to("/mnt/scratch/agentdb.sqlite3")

        with (
            patch.object(scratch, "_HOST_TMP", host_tmp),
            patch.object(scratch, "_HOST_PROC", host_proc),
            patch.dict(os.environ, {"TEATREE_HOST_TMP": "/mnt/scratch"}),
        ):
            sweep = scratch.resolve_scratch_sweep()
            sweep = ScratchSweep(
                root=sweep.root, retention_days=3, probe_root=sweep.probe_root, proc_root=sweep.proc_root
            )
            entry = next(e for e in sweep.plan().entries if e.path == str(held))

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_a_half_mounted_host_view_falls_back_to_this_venues_own_pair(self) -> None:
        host_tmp = Path(self.enterContext(TemporaryDirectory()))

        with patch.object(scratch, "_HOST_TMP", host_tmp), patch.object(scratch, "_HOST_PROC", host_tmp / "absent"):
            fallback = scratch.resolve_scratch_sweep()

        assert fallback.root == Path("/tmp")
        assert fallback.proc_root == scratch._VENUE_PROC
        assert fallback.probe_root is None

    def test_an_explicit_root_is_swept_against_this_venues_own_process_table(self) -> None:
        host_tmp = Path(self.enterContext(TemporaryDirectory()))
        host_proc = Path(self.enterContext(TemporaryDirectory()))

        with patch.object(scratch, "_HOST_TMP", host_tmp), patch.object(scratch, "_HOST_PROC", host_proc):
            explicit = scratch.resolve_scratch_sweep(str(self.root))

        assert explicit.root == self.root
        assert explicit.proc_root == scratch._VENUE_PROC
        assert explicit.probe_root is None

    def test_sweep_scratch_applies_only_when_asked(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=9, size=64)

        # The synthetic table stands in for this venue's own /proc: a real one
        # decides the outcome differently on a host than in a container.
        with patch.object(scratch, "_VENUE_PROC", self.proc):
            planned = scratch.sweep_scratch(configured_root=str(self.root), retention_days=3, apply=False)
            assert planned.candidate_bytes == 64
            assert stale.exists()

            applied = scratch.sweep_scratch(configured_root=str(self.root), retention_days=3, apply=True)
        assert applied.reclaimed_bytes == 64
        assert not stale.exists()


class ScratchSweepDegradedReadTests(ScratchSweepTestCase):
    """Every read that can fail keeps the entry rather than removing it."""

    def test_an_unlistable_root_reports_no_entries_rather_than_raising(self) -> None:
        missing = ScratchSweep(root=self.root / "absent", retention_days=3, proc_root=self.proc)

        assert missing.plan().entries == ()

    def test_a_failed_worktree_read_removes_nothing(self) -> None:
        _scratch(self.root, "t3db.sqlite3", days=9, size=128)

        with patch.object(scratch, "_worktree_paths", return_value=None):
            plan = self.sweep().plan()

        assert plan.candidates == ()
        assert "registered-worktree read failed" in plan.probe_gap

    def test_a_worktree_read_that_raises_is_a_probe_gap_not_a_crash(self) -> None:
        _scratch(self.root, "t3db.sqlite3", days=9)

        with patch.object(Worktree.objects, "values_list", side_effect=RuntimeError("db gone")):
            plan = self.sweep().plan()

        assert plan.candidates == ()
        assert "registered-worktree read failed" in plan.probe_gap

    def test_a_removal_the_filesystem_refuses_is_not_counted_as_reclaimed(self) -> None:
        _scratch(self.root, "t3db.sqlite3", days=9, size=256)

        with patch.object(scratch.Path, "unlink", side_effect=OSError("read-only")):
            plan = self.sweep().apply()

        assert plan.reclaimed_bytes == 0

    def test_an_entry_that_cannot_be_stat_ed_is_sized_zero_and_kept(self) -> None:
        unreadable = _scratch(self.root, "t3db.sqlite3", days=9, size=32)

        with patch.object(scratch.Path, "lstat", side_effect=OSError("gone")):
            entry = self.entry_for(self.sweep().plan(), unreadable)

        assert entry.size_bytes == 0
        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason

    def test_a_directory_whose_children_vanish_mid_walk_is_still_sized(self) -> None:
        tree = self.root / "wt4081venv"
        tree.mkdir()
        (tree / "lib.so").write_bytes(b"y" * 512)
        _age(tree, days=9)
        real_lstat = scratch.Path.lstat

        vanished = OSError("the file went away mid-walk")

        def flaky(self_path: Path) -> object:
            if self_path.name == "lib.so":
                raise vanished
            return real_lstat(self_path)

        with patch.object(scratch.Path, "lstat", flaky):
            entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.size_bytes == 0

    def test_a_nested_directorys_lstat_failing_mid_walk_is_skipped_not_fatal(self) -> None:
        tree = self.root / "wt4081venv"
        nested_dir = tree / "subdir"
        nested_file = nested_dir / "file.txt"
        nested_dir.mkdir(parents=True)
        nested_file.write_bytes(b"x" * 100)
        _age(nested_file, days=9)
        _age(nested_dir, days=9)
        _age(tree, days=9)
        real_lstat = scratch.Path.lstat
        vanished = FileNotFoundError("vanished mid-walk")

        def flaky(self_path: Path) -> object:
            if self_path.name == "subdir":
                raise vanished
            return real_lstat(self_path)

        with patch.object(scratch.Path, "lstat", flaky):
            entry = self.entry_for(self.sweep().plan(), tree)

        # os.walk descends into `subdir` regardless (it uses scandir, not lstat,
        # to recurse), so the file inside still counts; only the directory
        # entry's OWN lstat call is skipped rather than aborting the walk.
        assert entry.size_bytes == 100
        assert entry.removable is True

    def test_an_entry_that_vanishes_between_the_tree_walk_and_the_ownership_check_is_kept(self) -> None:
        """The narrow race the two-lstat-call split creates.

        ``_tree_stats`` succeeds, then the SEPARATE top-level re-stat inside
        ``_staleness_reason`` fails because the entry vanished in between.
        Unreadable can never prove staleness.
        """
        flaky_entry = _scratch(self.root, "t3after", days=9, size=64)
        real_lstat = scratch.Path.lstat
        calls = {"n": 0}
        vanished = OSError("vanished after the tree walk")

        def flaky(self_path: Path) -> object:
            if self_path == flaky_entry:
                calls["n"] += 1
                # _tree_stats's own explicit lstat() PLUS the internal lstat()
                # inside Path.is_symlink() both land here first; only the LATER
                # call from _staleness_reason's separate re-stat should fail.
                if calls["n"] > 2:
                    raise vanished
            return real_lstat(self_path)

        with patch.object(scratch.Path, "lstat", flaky):
            entry = self.entry_for(self.sweep().plan(), flaky_entry)

        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason

    def test_gigabyte_scratch_is_reported_in_binary_units(self) -> None:
        assert ScratchEntry(path="p", size_bytes=2 * 1024**3, age_days=9, removable=True, reason="").size_human == (
            "2.0GiB"
        )
        assert ScratchEntry(path="p", size_bytes=512, age_days=9, removable=True, reason="").size_human == "512B"


class TreeWideStalenessTests(ScratchSweepTestCase):
    """Cold-review finding #1: staleness must read the WHOLE tree, not just the top entry.

    A directory's own mtime moves only when an entry is added/removed/renamed
    DIRECTLY inside it — writing new bytes into an existing nested file never
    touches it. The pre-fix code read only the top-level ``lstat()``, so an
    old-looking directory whose deep content was written a moment ago still
    read as stale and ``apply()`` deleted a live working tree.
    """

    def test_a_nested_file_written_after_the_top_level_dir_survives_planning(self) -> None:
        tree = self.root / "wt4081venv"
        nested_dir = tree / "src"
        live_file = nested_dir / "existing.py"
        nested_dir.mkdir(parents=True)
        live_file.write_text("initial content")
        # Age EVERY node first, including the file — creating the file/dir
        # structure is what bumps a parent's own mtime, so that must happen
        # BEFORE aging, not after (else the parent looks "fresh" for the wrong
        # reason and the top-level-only bug this test targets never fires).
        _age(live_file, days=9)
        _age(nested_dir, days=9)
        _age(tree, days=9)
        # NOW simulate "content written a moment ago": REWRITE the EXISTING
        # file. Overwriting an existing file's content touches only the file's
        # own mtime — its containing directory's entry list is unchanged, so
        # neither `nested_dir` nor `tree` moves. This is exactly what the
        # pre-fix top-level-only check could not see.
        live_file.write_text("fresh content, written just now")

        entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.removable is False
        assert "younger than" in entry.reason

    def test_apply_never_deletes_a_tree_whose_newest_content_was_written_seconds_ago(self) -> None:
        """The review's own repro shape: apply() must not delete live work."""
        tree = self.root / "board3841"
        live_file = tree / "existing.txt"
        tree.mkdir()
        live_file.write_text("initial content")
        # As above: age everything first (creating the file bumps tree's own
        # mtime, so that must happen before aging), then overwrite the
        # EXISTING file's content — a rewrite alone never touches tree's mtime.
        _age(live_file, days=9)
        _age(tree, days=9)
        live_file.write_text("rewritten zero seconds ago")

        plan = self.sweep().apply()

        assert tree.exists()
        assert live_file.exists()
        assert live_file.read_text() == "rewritten zero seconds ago"
        assert plan.reclaimed_bytes == 0

    def test_apply_own_recheck_catches_a_nested_write_between_planning_and_deletion(self) -> None:
        """Isolates apply()'s OWN re-walk from plan()'s guard (#4165 review finding #1).

        A genuinely-stale plan is frozen (mirroring
        ``test_apply_skips_an_entry_touched_between_the_plan_and_the_unlink``), then a
        NESTED file is rewritten after that snapshot — invisible to a re-check that
        only re-stats the top-level entry, caught only by a re-walk of the whole tree.
        """
        tree = self.root / "board3841"
        live_file = tree / "existing.txt"
        tree.mkdir()
        live_file.write_text("initial content")
        _age(live_file, days=9)
        _age(tree, days=9)

        sweep = self.sweep()
        cleared = sweep.plan()
        assert self.entry_for(cleared, tree).removable is True

        # The box provisions continuously: a nested write lands after the plan
        # was taken. Freezing the plan isolates apply()'s OWN re-walk as the
        # only thing that can still catch it.
        live_file.write_text("rewritten between plan and unlink")

        with patch.object(ScratchSweep, "plan", return_value=cleared):
            applied = sweep.apply()

        assert tree.exists()
        assert live_file.read_text() == "rewritten between plan and unlink"
        assert applied.reclaimed_bytes == 0

    def test_a_tree_that_is_genuinely_idle_throughout_is_still_removable(self) -> None:
        """Companion: the tree-wide check does not over-protect a truly stale tree."""
        tree = self.root / "wt4081venv"
        nested = tree / "src" / "old_file.py"
        nested.parent.mkdir(parents=True)
        nested.write_text("stale content")
        _age(nested, days=9)
        _age(nested.parent, days=9)
        _age(tree, days=9)

        entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.removable is True


class MmapAndUnixSocketLivenessTests(ScratchSweepTestCase):
    """Cold-review finding #2: the open-file guard must see mmap and bound sockets.

    Neither shows up under a per-pid ``fd`` walk: an mmap'd file survives an
    ``fd`` close and is visible only via ``map_files``; a bound AF_UNIX socket's
    fd reads back as ``socket:[inode]`` and its bind path is only in that pid's
    own ``net/unix`` table.
    """

    def test_an_mmapped_file_with_no_open_fd_is_never_removable(self) -> None:
        mapped = _scratch(self.root, "shared.db", days=9)
        (self.proc / "555" / "map_files").mkdir(parents=True)
        # Deliberately NO fd/ dir at all — the fd was closed after mmap().
        (self.proc / "555" / "map_files" / "7f0000-7f1000").symlink_to(mapped)

        entry = self.entry_for(self.sweep().plan(), mapped)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_apply_never_deletes_an_mmapped_file(self) -> None:
        mapped = _scratch(self.root, "shared.db", days=9)
        (self.proc / "555" / "map_files").mkdir(parents=True)
        (self.proc / "555" / "map_files" / "7f0000-7f1000").symlink_to(mapped)

        self.sweep().apply()

        assert mapped.exists()

    def test_a_bound_unix_socket_path_is_never_removable(self) -> None:
        sock = self.root / "worker.sock"
        sock.write_bytes(b"")
        _age(sock, days=9)
        _listening_socket(self.proc / "555", str(sock))

        entry = self.entry_for(self.sweep().plan(), sock)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_an_abstract_namespace_socket_name_is_not_treated_as_a_filesystem_path(self) -> None:
        """The abstract name TEXTUALLY CONTAINS the stale path, so the filter alone decides.

        Paired with the ``/``-prefixed control directly above: same path, real bind →
        ``removable is False``. Here the only difference is the leading ``@``, so a
        ``lstrip("@")``-style implementation would keep the entry and this goes red —
        an assertion against an unrelated path holds even with the parser deleted.
        """
        stale = _scratch(self.root, "held.sock", days=9)
        _listening_socket(self.proc / "555", f"@{stale}")

        entry = self.entry_for(self.sweep().plan(), stale)

        assert entry.removable is True

    def test_a_socket_bound_only_in_the_pids_own_namespace_is_still_seen(self) -> None:
        """The namespace-mismatch repro: the bare ``proc_root/net/unix`` answers for the WRONG namespace.

        ``/proc/net`` is a magic symlink to ``self/net``, resolved against the
        READING process, so a container reading a bind-mounted host ``/proc``
        gets its own empty socket table back — successfully, with no exception
        for a fail-closed path to catch. Only the pid-scoped table sees the
        socket that is actually holding this entry alive.
        """
        sock = self.root / "worker.sock"
        sock.write_bytes(b"")
        _age(sock, days=9)
        _net_unix(self.proc)  # the ambiguous global path: readable, and reports nothing
        (self.proc / "111" / "fd").mkdir(parents=True)  # a pid that answers, holding nothing
        _net_unix(self.proc / "555", str(sock))

        entry = self.entry_for(self.sweep().plan(), sock)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_the_ambiguous_global_net_unix_is_never_consulted(self) -> None:
        """A decoy bind under ``proc_root/net/unix`` must not save an entry no pid holds."""
        stale = _scratch(self.root, "decoy.sock", days=9)
        _net_unix(self.proc, str(stale))
        (self.proc / "111" / "fd").mkdir(parents=True)  # a pid that answers, holding nothing

        entry = self.entry_for(self.sweep().plan(), stale)

        assert entry.removable is True

    def test_an_unreadable_net_unix_beside_an_answering_fd_is_an_accepted_residual(self) -> None:
        """Site F's residual: one namespace's socket table is a per-pid gap, not a probe-wide blind.

        Pooled across pids an absent namespace cannot be told from a socket-less
        one, so the fail-closed contract is carried by the access-gated sources.
        """
        stale = _scratch(self.root, "t3after", days=9, size=2048)
        answering = self.blind_proc / "777"
        (answering / "fd").mkdir(parents=True)
        (answering / "fd" / "3").symlink_to(self.elsewhere / "unrelated")
        # A directory where the table should be, so read_text() raises IsADirectoryError.
        (answering / "net" / "unix").mkdir(parents=True)

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.probe_gap == ""
        assert self.entry_for(plan, stale).removable is True

    def test_a_readable_socket_table_alone_never_vouches_for_a_blind_probe(self) -> None:
        """``<pid>/net/unix`` is 0444 where ``fd``/``map_files`` are 0500 behind ptrace.

        So the socket table answers for every pid whatever this uid can actually
        reach — including the measured container shape where no fd, cwd or
        map_files resolves at all. Counting it as a witness would retire the C2
        blindness guard on any real ``/proc`` and let the sweep delete files it
        cannot see the holders of.
        """
        stale = _scratch(self.root, "t3after", days=9, size=64)
        _net_unix(self.blind_proc / "777", str(self.root / "some-other.sock"))

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap
        assert stale.exists()


class AdHocGitRepoTests(ScratchSweepTestCase):
    """Cold-review finding #3: an unregistered git checkout must never be removable.

    ``_worktree_paths()`` only sees rows the DB was told about. An agent that
    clones a repo by hand under the swept root — never registered as a teatree
    ``Worktree`` — was invisible to that guard entirely.
    """

    def test_a_top_level_ad_hoc_clone_is_never_removable(self) -> None:
        clone = self.root / "manual-clone"
        (clone / ".git").mkdir(parents=True)
        _age(clone / ".git", days=9)
        _age(clone, days=9)

        entry = self.entry_for(self.sweep().plan(), clone)

        assert entry.removable is False
        assert entry.reason == "holds a git repository"

    def test_a_nested_ad_hoc_clone_protects_its_whole_scratch_dir(self) -> None:
        scratch_dir = self.root / "rev3970"
        repo = scratch_dir / "checkout"
        (repo / ".git").mkdir(parents=True)
        _age(repo / ".git", days=9)
        _age(repo, days=9)
        _age(scratch_dir, days=9)

        entry = self.entry_for(self.sweep().plan(), scratch_dir)

        assert entry.removable is False
        assert entry.reason == "holds a git repository"

    def test_apply_never_deletes_an_ad_hoc_clone(self) -> None:
        clone = self.root / "manual-clone"
        (clone / ".git").mkdir(parents=True)
        _age(clone / ".git", days=9)
        _age(clone, days=9)

        self.sweep().apply()

        assert clone.exists()
        assert (clone / ".git").exists()

    def test_a_worktree_style_git_file_marker_also_protects(self) -> None:
        """A worktree's ``.git`` is a FILE (pointer), not a directory — both must count."""
        checkout = self.root / "wt-style"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt-style\n")
        _age(checkout / ".git", days=9)
        _age(checkout, days=9)

        entry = self.entry_for(self.sweep().plan(), checkout)

        assert entry.removable is False
        assert entry.reason == "holds a git repository"


class UnsearchableTopLevelEntryTests(ScratchSweepTestCase):
    """Cold-review CRITICAL C1: an entry this uid cannot search must not crash the sweep.

    ``(path / ".git").exists()`` re-raises ``EACCES`` rather than swallowing it
    (only ``ENOENT``/``ENOTDIR``/``EBADF``/``ELOOP`` are ignorable), so a
    mode-0700 directory owned by another uid used to crash ``plan()`` outright
    — and ``_inert_plan`` walks the same tree, so even the retention-disabled
    path crashed too.
    """

    @skip_if_root
    def test_plan_does_not_crash_on_a_top_level_dir_this_uid_cannot_search(self) -> None:
        blocked = self.root / "another-uids-scratch"
        blocked.mkdir()
        _age(blocked, days=9)
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)

        entry = self.entry_for(self.sweep().plan(), blocked)

        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason

    @skip_if_root
    def test_the_git_probes_fail_open_stays_masked_by_the_unscannable_walk(self) -> None:
        """Site E's accepted residual, pinned as a COUPLING so neither half can move alone.

        ``(path / ".git").exists()`` swallows the EACCES into ``holds_git_repo =
        False`` — a fail-OPEN on its own. It is safe only because the same
        unsearchable directory forces ``newest_mtime`` to None in the very same
        walk. Assert both halves together: a future edit that fixes the walk to
        report a time, or drops the onerror handler, unmasks the fail-open.
        """
        blocked = self.root / "another-uids-scratch"
        blocked.mkdir()
        _age(blocked, days=9)
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)

        stats = scratch._tree_stats(blocked)

        assert stats.holds_git_repo is False
        assert stats.newest_mtime is None

    @skip_if_root
    def test_the_retention_disabled_path_does_not_crash_either(self) -> None:
        blocked = self.root / "another-uids-scratch"
        blocked.mkdir()
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)

        plan = self.sweep(retention_days=0).plan()

        assert plan.entries
        assert plan.candidates == ()

    @skip_if_root
    def test_apply_does_not_crash_on_a_top_level_dir_this_uid_cannot_search(self) -> None:
        """``apply()`` calls ``plan()`` internally, so the crash site is reached either way.

        The meaningful assertion is that it does not raise, not whether the
        (real, chmod-blocked) ``rmtree`` happens to succeed: an unsearchable
        directory also can't be recursively DELETED, so "still exists after
        apply()" would pass on unfixed code too, for the wrong reason.
        """
        blocked = self.root / "another-uids-scratch"
        blocked.mkdir()
        _age(blocked, days=9)
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)

        plan = self.sweep().apply()  # must not raise

        assert plan.applied is True


class UnsearchableButReadableSubdirectoryTests(ScratchSweepTestCase):
    """A 0444 subdirectory is LISTABLE, so ``os.walk``'s own error hook never fires.

    Mode 0444 grants read (scandir succeeds, ``onerror`` is not called) but denies
    search, so every child ``lstat`` raises EACCES. That is a different door from
    the unscannable 0000 case above: the tree's newest mtime silently stays at the
    parent's, and content written a second ago classifies as nine days idle.
    """

    @skip_if_root
    def test_a_fresh_file_under_an_unsearchable_dir_is_not_reported_stale(self) -> None:
        tree = self._tree_with_a_fresh_file_under_an_unsearchable_dir()

        entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason
        assert "9.0" not in entry.reason, "an unreadable mtime must not be reported as a measured age"

    def test_apply_never_deletes_a_tree_whose_child_stats_are_denied(self) -> None:
        """The same claim as ``plan()`` above, proven without ``chmod``.

        A REAL 0444 directory also stops ``shutil.rmtree`` from recursing, so
        "apply() didn't delete it" passes on UNFIXED code too — the OS's permission
        wall, not this module's fail-closed logic, does the protecting. Denying only
        the ``lstat`` leaves the real tree fully deletable, so nothing but the code's
        own guard decides the outcome.
        """
        tree = self.root / "wt-scratch"
        denied = tree / "unsearchable"
        denied.mkdir(parents=True)
        (denied / "fresh.txt").write_bytes(b"just written")
        _age(denied / "fresh.txt", days=9)
        _age(denied, days=9)
        _age(tree, days=9)
        real_lstat = Path.lstat

        def deny_under_denied(self: Path, **kwargs: object) -> os.stat_result:
            if denied in self.parents:
                raise PermissionError(13, "simulated: search denied on the parent directory")
            return real_lstat(self, **kwargs)

        with patch.object(Path, "lstat", deny_under_denied):
            self.sweep().apply()

        assert tree.exists()

    def _tree_with_a_fresh_file_under_an_unsearchable_dir(self) -> Path:
        """A stale tree whose only fresh content sits behind a readable-but-unsearchable dir.

        The rewrite is content-only and lands BEFORE the ``chmod``: creating a new
        entry afterwards would bump the unsearchable directory's own mtime, which
        the parent's walk CAN read — letting the unfixed code detect the freshness
        by accident and pass the test vacuously.
        """
        tree = self.root / "wt-scratch"
        blocked = tree / "unsearchable"
        nested = blocked / "fresh.txt"
        blocked.mkdir(parents=True)
        nested.write_bytes(b"old content")
        _age(nested, days=9)
        _age(blocked, days=9)
        _age(tree, days=9)
        nested.write_bytes(b"just written")
        blocked.chmod(0o444)
        self.addCleanup(blocked.chmod, 0o755)
        return tree


class UnscannableSubdirectoryTests(ScratchSweepTestCase):
    """Cold-review residual finding: os.walk's default onerror=None silently under-reports.

    Same shape as the original tree-wide-mtime finding, one level deeper: a
    subdirectory this uid cannot scandir into is skipped rather than treated
    as "cannot prove staleness", so content written a moment ago underneath it
    silently never counts toward the tree's newest mtime.
    """

    @skip_if_root
    def test_an_unscannable_nested_directory_blinds_the_whole_entry(self) -> None:
        tree, _blocked = self._tree_with_a_freshly_rewritten_but_unscannable_subdir()

        entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason

    def test_apply_never_deletes_a_tree_reported_as_having_an_unscannable_subdirectory(self) -> None:
        """Same claim as the ``plan()`` test above, proven without ``chmod``.

        A REAL EACCES on ``blocked`` would also make ``shutil.rmtree`` unable to
        recurse into it, so "apply() didn't delete it" would pass on UNFIXED code
        too — the OS's own permission wall, not this module's fail-closed logic,
        would be doing the protecting. Simulating the scandir failure through
        ``os.walk`` directly (rather than real permissions) leaves the real tree
        fully deletable, so only the code's OWN guard — or its absence — decides
        the outcome.
        """
        tree = self.root / "wt-scratch"
        blocked = tree / "another-uids-subdir"
        blocked.mkdir(parents=True)
        _age(tree, days=9)
        _age(blocked, days=9)
        real_walk = os.walk

        def blind_to_blocked(path, *, onerror=None, followlinks=False):
            for root, dirs, files in real_walk(path, followlinks=followlinks):
                if Path(root) == blocked:
                    if onerror is not None:
                        onerror(OSError("simulated: cannot scandir this subdirectory"))
                    continue
                yield root, dirs, files

        with patch.object(scratch.os, "walk", side_effect=blind_to_blocked):
            self.sweep().apply()

        assert tree.exists()

    def _tree_with_a_freshly_rewritten_but_unscannable_subdir(self) -> tuple[Path, Path]:
        """A subdir aged old, then blinded, whose CONTENT was rewritten a moment ago.

        The rewrite must land BEFORE the ``chmod`` (writing through a 0o000 dir
        is impossible) and must be a content OVERWRITE of an EXISTING file, not
        a new file: creating a new entry bumps the containing dir's own mtime,
        which would let the OLD, unfixed code "detect" freshness by accident —
        via the directory's own mtime, not via actually scanning the subtree —
        and pass this test vacuously.
        """
        tree = self.root / "wt-scratch"
        blocked = tree / "another-uids-subdir"
        nested_file = blocked / "fresh.txt"
        blocked.mkdir(parents=True)
        nested_file.write_bytes(b"old content")
        _age(nested_file, days=9)
        _age(blocked, days=9)
        _age(tree, days=9)
        nested_file.write_bytes(b"just written")  # content-only: blocked's mtime is untouched
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        return tree, blocked


class OpenFileProbeStructuralBlindnessTests(ScratchSweepTestCase):
    """Cold-review CRITICAL C2: the open-file guard must fail CLOSED when structurally blind.

    Measured in the real container: fd readlinks 0 of 17, cwd EACCES, map_files
    EACCES, versus 17 of 17 / readable / readable on the host — every per-pid
    source unreadable for every pid, yet the old code returned an empty (not
    ``None``) frozenset, so the plan proceeded as though nothing were held. A
    single pid whose sources are ALL unreadable is the normal multi-user case
    (another uid's process) and must NOT blind the probe on its own — only
    when NOT ONE pid anywhere answers does the probe count as blind.
    """

    def test_when_no_pid_anywhere_answers_the_whole_probe_blinds(self) -> None:
        _scratch(self.root, "t3after", days=9, size=64)
        # A pid is visible (this is not "no processes"), but none of its
        # per-pid sources exist to read — the exact container-side shape.
        (self.blind_proc / "777").mkdir()

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap

    def test_apply_removes_nothing_when_the_probe_is_structurally_blind(self) -> None:
        stale = _scratch(self.root, "t3after", days=9, size=64)
        (self.blind_proc / "777").mkdir()

        self.sweep(proc_root=self.blind_proc).apply()

        assert stale.exists()

    def test_a_lone_readable_pid_among_unreadable_ones_still_answers(self) -> None:
        """The ordinary multi-user case: most pids belong to another uid and answer nothing.

        That alone must never blind the probe.
        """
        held = _scratch(self.root, "still-open.db", days=9)
        (self.blind_proc / "111").mkdir()  # another uid's pid: nothing readable
        readable = self.blind_proc / "222" / "fd"
        readable.mkdir(parents=True)
        (readable / "3").symlink_to(held)

        entry = self.entry_for(self.sweep(proc_root=self.blind_proc).plan(), held)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_a_lone_readable_cwd_among_unreadable_pids_still_answers(self) -> None:
        """Cwd alone (no fd, no map_files) must count as an answer too."""
        stale = _scratch(self.root, "t3after", days=9, size=64)
        (self.blind_proc / "111").mkdir()  # another uid's pid: nothing readable
        (self.blind_proc / "222").mkdir()
        (self.blind_proc / "222" / "cwd").symlink_to(self.root)

        plan = self.sweep(proc_root=self.blind_proc).plan()

        # Not blind (222's cwd answered) — the entry is decided on its own
        # merits (staleness), not forced KEEP by a blind probe.
        entry = self.entry_for(plan, stale)
        assert "open-file probe unsighted" not in plan.probe_gap
        assert entry.removable is True


class ResolutionWitnessTests(ScratchSweepTestCase):
    """Verdict 1001: the witness is RESOLUTION, never mere listability.

    Measured in the worker's own image at uid 1001 against a bind-mounted host
    ``/proc``: 26-31 pids present an fd directory this uid can LIST while every
    ``readlink()`` inside it raises — all of them uid 1001, the same uid whose
    scratch the sweep deletes. Counting that listing as an answer reported an
    unreadable world as an empty one, and ``apply()`` deleted a live process's
    open file.
    """

    def test_a_listable_fd_dir_that_resolves_nothing_blinds_the_whole_probe(self) -> None:
        held = _scratch(self.root, "still-open.db", days=9, size=64)
        fd_dir = self.blind_proc / "777" / "fd"
        fd_dir.mkdir(parents=True)
        # A REGULAR FILE where a symlink belongs: iterdir() lists it, readlink()
        # raises EINVAL — the in-container EACCES shape, reproducible anywhere.
        (fd_dir / "3").write_bytes(b"")

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap
        assert self.entry_for(plan, held).removable is False

    def test_the_same_entry_as_a_resolvable_symlink_is_the_control_that_keeps_it(self) -> None:
        """Control for the test above: resolution succeeds, so the probe answers.

        Without it, "kept" is indistinguishable from the blind path's
        keep-everything and the RED test proves nothing.
        """
        held = _scratch(self.root, "still-open.db", days=9, size=64)
        fd_dir = self.blind_proc / "777" / "fd"
        fd_dir.mkdir(parents=True)
        (fd_dir / "3").symlink_to(held)

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.probe_gap == ""
        entry = self.entry_for(plan, held)
        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_a_blind_source_blinds_the_probe_even_beside_an_answering_pid(self) -> None:
        """The failure is PROBE-WIDE: an unknowable pid may hold ANY candidate path.

        A sibling pid that answers says nothing about what the blind one holds,
        so no partial answer is available to fall back on.
        """
        stale = _scratch(self.root, "t3after", days=9, size=64)
        blind_fd = self.proc / "777" / "fd"  # self.proc already carries an answering pid
        blind_fd.mkdir(parents=True)
        (blind_fd / "3").write_bytes(b"")

        plan = self.sweep().plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap
        assert stale.exists()

    def test_an_empty_but_readable_fd_dir_is_an_answer_not_a_blind(self) -> None:
        """Every kernel thread presents one.

        Scoring it a non-answer would blind the probe on every real ``/proc`` and
        leave the sweep permanently inert.
        """
        stale = _scratch(self.root, "t3after", days=9, size=64)
        (self.blind_proc / "777" / "fd").mkdir(parents=True)

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.probe_gap == ""
        assert self.entry_for(plan, stale).removable is True

    def test_an_answering_fd_dir_does_not_excuse_a_blind_map_files(self) -> None:
        """Control for the pair above: the two gated sources are judged separately."""
        stale = _scratch(self.root, "t3after", days=9, size=64)
        pid = self.blind_proc / "777"
        (pid / "fd").mkdir(parents=True)
        (pid / "map_files").mkdir()
        (pid / "map_files" / "7f0000-7f1000").write_bytes(b"")

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert "open-file probe unsighted" in plan.probe_gap
        assert stale.exists()

    def test_a_process_table_with_no_numeric_pid_is_not_a_procfs_and_blinds(self) -> None:
        """A live procfs always carries pid 1.

        Zero numeric entries means the mount is not a process table at all — the
        shape 40 of the pre-existing tests in this file had.
        """
        stale = _scratch(self.root, "t3after", days=9, size=64)
        (self.blind_proc / "sys").mkdir()  # listable, and nothing numeric in it

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.candidates == ()
        assert "open-file probe unsighted" in plan.probe_gap
        assert stale.exists()

    def test_one_numeric_answering_pid_is_the_control_for_the_empty_table(self) -> None:
        stale = _scratch(self.root, "t3after", days=9, size=64)
        (self.blind_proc / "sys").mkdir()
        _answering_pid(self.blind_proc, self.elsewhere / "held-elsewhere")

        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert plan.probe_gap == ""
        assert self.entry_for(plan, stale).removable is True

    def test_apply_reclaims_nothing_when_a_pid_resolves_nothing(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=9, size=64)
        fd_dir = self.blind_proc / "777" / "fd"
        fd_dir.mkdir(parents=True)
        (fd_dir / "3").write_bytes(b"")

        plan = self.sweep(proc_root=self.blind_proc).apply()

        assert plan.reclaimed_bytes == 0
        assert stale.exists()


class UnsightedProbeRefusesTests(ScratchSweepTestCase):
    """A probe that could not look REFUSES; it never reports a clean 0.00 GB no-op.

    ``reclaimed 0.00 GB`` from a blinded probe is byte-identical to the same line
    on an already-clean box, so an armed-but-inoperative lane ships unnoticed.
    """

    def setUp(self) -> None:
        super().setUp()
        self.victim = _scratch(self.root, "t3db.sqlite3", days=9, size=2048)
        holder_fd = self.blind_proc / "202" / "fd"
        holder_fd.mkdir(parents=True)
        (holder_fd / "3").symlink_to(self.victim)
        _answering_pid(self.blind_proc, self.elsewhere / "held-elsewhere")
        holder_fd.chmod(0o000)
        self.addCleanup(holder_fd.chmod, 0o700)

    def test_apply_leaves_a_file_the_unreadable_pid_holds_open(self) -> None:
        plan = self.sweep(proc_root=self.blind_proc).apply()

        assert self.victim.exists()
        assert plan.refused is True
        assert plan.reclaimed_bytes == 0

    def test_the_summary_says_refused_rather_than_reclaimed(self) -> None:
        plan = self.sweep(proc_root=self.blind_proc).apply()

        assert "REFUSED" in plan.summary
        assert "reclaimed 0.00 GB" not in plan.summary

    def test_the_gap_names_the_coverage_counts_and_the_arming_precondition(self) -> None:
        plan = self.sweep(proc_root=self.blind_proc).plan()

        assert "1 of 2 pid(s) unknowable" in plan.probe_gap
        assert "ptrace" in plan.probe_gap

    def test_the_deliberate_off_switch_is_not_a_refusal(self) -> None:
        plan = self.sweep(retention_days=0, proc_root=self.blind_proc).plan()

        assert plan.refused is False
        assert "retention disabled" in plan.probe_gap

    def test_a_failed_worktree_read_refuses_too(self) -> None:
        with patch.object(scratch, "_worktree_paths", return_value=None):
            plan = self.sweep().apply()

        assert plan.refused is True
        assert self.victim.exists()

    def test_a_sighted_probe_still_reclaims_the_positive_control(self) -> None:
        plan = self.sweep().apply()

        assert plan.refused is False
        assert not self.victim.exists()


class SymlinkedRootSpellingTests(ScratchSweepTestCase):
    """A symlinked component above the entry must not make a held path compare unequal."""

    def setUp(self) -> None:
        super().setUp()
        self.entry = self.root / "wt4081venv"
        self.entry.mkdir()
        self.held = self.entry / "python"
        self.held.write_bytes(b"x" * 64)
        _age(self.held, days=9)
        _age(self.entry, days=9)
        self.linked_root = Path(self.enterContext(TemporaryDirectory())) / "via-link"
        self.linked_root.symlink_to(self.root)
        view = ProcessTableView(held=frozenset({str(self.held)}), answered_pids=1, unknowable_pids=0)
        self.enterContext(patch.object(scratch, "held_paths", return_value=view))

    def _plan_through(self, root: Path) -> ScratchSweepPlan:
        return ScratchSweep(root=root, retention_days=3, proc_root=self.proc).plan()

    def test_a_root_spelled_through_a_symlink_still_sees_the_holder(self) -> None:
        plan = self._plan_through(self.linked_root)

        assert self.entry_for(plan, self.linked_root / "wt4081venv").removable is False

    def test_the_realpath_spelling_is_the_control_and_is_kept_either_way(self) -> None:
        plan = self._plan_through(self.root)

        assert self.entry_for(plan, self.entry).removable is False


class NonAbsenceReadFailureTests(ScratchSweepTestCase):
    """Only ENOENT is an absence; every other errno is a blind spot that keeps the entry."""

    def test_a_child_lstat_raising_a_non_enoent_error_keeps_the_tree(self) -> None:
        tree = self.root / "wt4081venv"
        tree.mkdir()
        (tree / "lib.so").write_bytes(b"y" * 512)
        _age(tree, days=9)
        real_lstat = scratch.Path.lstat
        eloop = OSError("too many levels of symbolic links")

        def flaky(self_path: Path) -> object:
            if self_path.name == "lib.so":
                raise eloop
            return real_lstat(self_path)

        with patch.object(scratch.Path, "lstat", flaky):
            entry = self.entry_for(self.sweep().plan(), tree)

        assert entry.removable is False
        assert "cannot prove it is stale" in entry.reason


class HostMountSubPathPairingTests(ScratchSweepTestCase):
    """A configured root UNDER the host mount is the host view, not this venue's."""

    def setUp(self) -> None:
        super().setUp()
        self.host_tmp = Path(self.enterContext(TemporaryDirectory()))
        self.host_proc = Path(self.enterContext(TemporaryDirectory()))

    def _resolved(self, configured: str) -> ScratchSweep:
        with (
            patch.object(scratch, "_HOST_TMP", self.host_tmp),
            patch.object(scratch, "_HOST_PROC", self.host_proc),
            patch.dict(os.environ, {scratch._HOST_TMP_ENV: "/real/host/tmp"}),
        ):
            return scratch.resolve_scratch_sweep(configured)

    def test_a_sub_path_of_the_host_mount_reads_the_host_process_table(self) -> None:
        resolved = self._resolved(str(self.host_tmp / "agent-scratch"))

        assert resolved.proc_root == self.host_proc
        assert resolved.probe_root == Path("/real/host/tmp/agent-scratch")

    def test_the_mount_point_itself_is_unchanged(self) -> None:
        resolved = self._resolved("")

        assert resolved.root == self.host_tmp
        assert resolved.proc_root == self.host_proc
        assert resolved.probe_root == Path("/real/host/tmp")
