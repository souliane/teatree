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


class ScratchSweepTestCase(TestCase):
    """Shared temp root + a synthetic process table the sweep reads instead of /proc."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.proc = Path(self.enterContext(TemporaryDirectory()))

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
        (scratch_dir / "nested").mkdir(parents=True)
        _age(scratch_dir, days=9)
        (self.proc / "77").mkdir()
        (self.proc / "77" / "cwd").symlink_to(scratch_dir / "nested")

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
        (stale / "lib.so").write_bytes(b"y" * 1024)
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

        with patch.object(scratch, "_HOST_TMP", host_tmp), patch.object(scratch, "_HOST_PROC", host_proc):
            paired = scratch.resolve_scratch_sweep()

        assert paired.root == host_tmp
        assert paired.proc_root == host_proc
        assert paired.probe_root == Path("/tmp")

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

    def test_gigabyte_scratch_is_reported_in_binary_units(self) -> None:
        assert ScratchEntry(path="p", size_bytes=2 * 1024**3, age_days=9, removable=True, reason="").size_human == (
            "2.0GiB"
        )
        assert ScratchEntry(path="p", size_bytes=512, age_days=9, removable=True, reason="").size_human == "512B"
