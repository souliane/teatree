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
from teatree.core.retention.scratch import ScratchEntry, ScratchSweep, ScratchSweepPlan


def _age(path: Path, *, days: float) -> None:
    stamp = timezone.now().timestamp() - days * 86400
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _scratch(root: Path, name: str, *, days: float, size: int = 16) -> Path:
    entry = root / name
    entry.write_bytes(b"x" * size)
    _age(entry, days=days)
    return entry


def _readable_net_unix(proc_root: Path) -> None:
    """Give a synthetic proc root a real system's ``net/unix`` — header, no sockets.

    Every genuine ``/proc`` has this file; a bare fixture directory does not, and
    an unreadable ``net/unix`` is a deliberate probe-blinding signal elsewhere in
    this module (see ``ScratchSweepDegradedReadTests``). Any fixture whose sweep
    reaches ``plan()``/``apply()`` needs this so it exercises the guards under
    test rather than the (separately, explicitly tested) unreadable-source path.
    """
    (proc_root / "net").mkdir(parents=True, exist_ok=True)
    (proc_root / "net" / "unix").write_text(
        "Num       RefCount Protocol Flags    Type St Inode Path\n", encoding="utf-8"
    )


class ScratchSweepTestCase(TestCase):
    """Shared temp root + a synthetic process table the sweep reads instead of /proc."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.proc = Path(self.enterContext(TemporaryDirectory()))
        _readable_net_unix(self.proc)

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
        assert "open-file probe unreadable" in plan.probe_gap
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
        _readable_net_unix(host_proc)
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
        assert fallback.proc_root == Path("/proc")
        assert fallback.probe_root is None

    def test_an_explicit_root_is_swept_against_this_venues_own_process_table(self) -> None:
        host_tmp = Path(self.enterContext(TemporaryDirectory()))
        host_proc = Path(self.enterContext(TemporaryDirectory()))

        with patch.object(scratch, "_HOST_TMP", host_tmp), patch.object(scratch, "_HOST_PROC", host_proc):
            explicit = scratch.resolve_scratch_sweep(str(self.root))

        assert explicit.root == self.root
        assert explicit.proc_root == Path("/proc")
        assert explicit.probe_root is None

    def test_sweep_scratch_applies_only_when_asked(self) -> None:
        stale = _scratch(self.root, "t3db.sqlite3", days=9, size=64)

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
        vanished = OSError("vanished mid-walk")

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
    fd reads back as ``socket:[inode]`` and its bind path is only in
    ``/proc/net/unix``, a process-table-wide source, not a per-pid one.
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
        (self.proc / "net").mkdir(parents=True, exist_ok=True)
        (self.proc / "net" / "unix").write_text(
            "Num       RefCount Protocol Flags    Type St Inode Path\n"
            f"0000000000000000: 00000002 00000000 00000000 0001 01 12345 {sock}\n",
            encoding="utf-8",
        )

        entry = self.entry_for(self.sweep().plan(), sock)

        assert entry.removable is False
        assert entry.reason == "open by a live process"

    def test_an_abstract_namespace_socket_name_is_not_treated_as_a_filesystem_path(self) -> None:
        stale = _scratch(self.root, "unrelated.sock", days=9)
        (self.proc / "net").mkdir(parents=True, exist_ok=True)
        (self.proc / "net" / "unix").write_text(
            "Num       RefCount Protocol Flags    Type St Inode Path\n"
            "0000000000000000: 00000002 00000000 00000000 0001 01 12345 @abstract-name\n",
            encoding="utf-8",
        )

        entry = self.entry_for(self.sweep().plan(), stale)

        assert entry.removable is True

    def test_an_unreadable_net_unix_blinds_the_whole_probe(self) -> None:
        """Process-table-wide source: a read failure here is fail-closed like proc_root itself."""
        _scratch(self.root, "t3after", days=9, size=2048)
        # setUp already gave this fixture a readable net/unix FILE; replace it with a
        # directory at the same path, so read_text() raises IsADirectoryError.
        (self.proc / "net" / "unix").unlink()
        (self.proc / "net" / "unix").mkdir()

        plan = self.sweep().plan()

        assert plan.candidates == ()
        assert "open-file probe unreadable" in plan.probe_gap


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
