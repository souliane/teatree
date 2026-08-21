"""Mount-point boundary detection (souliane/teatree#4368).

Functional: the live ``/proc/self/mountinfo`` where an assertion can be made about
it, and real directories under ``tmp_path`` for the discriminator — two paths whose
``st_dev`` this test ASSERTS equal, declared as distinct mounts in a synthetic
table read through the real reader. That pair is the whole point: a bind mount is
same-device and still returns ``EXDEV``, so a guard keyed on ``st_dev`` passes it
and the move fails anyway.

A bind mount cannot be created without root, so the table is synthetic while every
path in it is real — which is what makes the ``st_dev`` assertion meaningful.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from teatree.utils.mount_points import (
    MountEntry,
    mount_boundary_between,
    mount_entry_for,
    mount_point_for,
    parse_mountinfo,
    read_mount_entries,
)

_SAMPLE = (
    "23 28 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw\n"
    "28 1 252:0 / / rw,relatime - ext4 /dev/mapper/hk-root rw\n"
)


@contextmanager
def _pinned_table(table_dir: Path, *mount_points: Path) -> Iterator[None]:
    """Run the real reader against a synthetic table declaring *mount_points* as mounts."""
    rows = [f"36 28 252:0 / {point} rw,relatime - ext4 /dev/mapper/hk-root rw" for point in mount_points]
    mountinfo = table_dir / "mountinfo"
    mountinfo.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with mock.patch("teatree.utils.mount_points._MOUNTINFO", mountinfo):
        yield


class TestParseMountinfo:
    def test_reads_mount_point_and_fstype(self) -> None:
        entries = parse_mountinfo(_SAMPLE)
        assert entries == (MountEntry(Path("/proc"), "proc"), MountEntry(Path("/"), "ext4"))

    def test_a_row_with_no_separator_or_too_few_fields_is_skipped(self) -> None:
        assert parse_mountinfo("garbage\n28 1 252:0 / /\n36 28 252:0 / /x rw - \n") == ()

    def test_octal_escapes_in_the_mount_point_are_decoded(self) -> None:
        entries = parse_mountinfo("36 28 252:0 / /mnt/my\\040disk rw - ext4 /dev/sda1 rw\n")
        assert entries[0].mount_point == Path("/mnt/my disk")


class TestMountEntryFor:
    def test_the_longest_covering_mount_point_wins(self) -> None:
        entries = (MountEntry(Path("/"), "ext4"), MountEntry(Path("/srv/a"), "xfs"))
        assert mount_entry_for(Path("/srv/a/b/c"), entries) == entries[1]

    def test_a_sibling_prefix_does_not_count_as_covering(self) -> None:
        entries = (MountEntry(Path("/"), "ext4"), MountEntry(Path("/srv/abc"), "xfs"))
        assert mount_entry_for(Path("/srv/abcdef"), entries) == entries[0]

    def test_a_later_row_over_mounts_an_earlier_one_on_the_same_point(self) -> None:
        entries = (MountEntry(Path("/mnt"), "ext4"), MountEntry(Path("/mnt"), "tmpfs"))
        assert mount_entry_for(Path("/mnt/x"), entries) == entries[1]

    def test_no_covering_mount_point_is_none(self) -> None:
        assert mount_entry_for(Path("/srv/x"), (MountEntry(Path("/other"), "ext4"),)) is None


class TestReadMountEntries:
    def test_the_live_table_places_proc_self_on_the_proc_mount(self) -> None:
        if not Path("/proc/self/mountinfo").exists():
            pytest.skip("no /proc/self/mountinfo in this venue")
        assert mount_point_for(Path("/proc/self")) == Path("/proc")

    def test_an_unreadable_table_is_unknown_not_a_guess(self, tmp_path: Path) -> None:
        with mock.patch("teatree.utils.mount_points._MOUNTINFO", tmp_path / "absent"):
            assert read_mount_entries() is None
            assert mount_point_for(tmp_path) is None
            assert mount_boundary_between(tmp_path, tmp_path.parent) is None


class TestMountBoundaryBetween:
    def test_two_same_device_paths_on_distinct_mounts_are_a_boundary(self, tmp_path: Path) -> None:
        # The discriminator: st_dev AGREES here, so a device-keyed guard would call
        # this move safe — and `git worktree move` would then fail with EXDEV.
        src, dst = tmp_path / "bind-a", tmp_path / "bind-b"
        src.mkdir()
        dst.mkdir()
        assert src.stat().st_dev == dst.stat().st_dev

        with _pinned_table(tmp_path, Path("/"), src, dst):
            assert mount_boundary_between(src, dst) == (src, dst)

    def test_paths_sharing_one_mount_point_are_not_a_boundary(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        with _pinned_table(tmp_path, Path("/"), tmp_path):
            assert mount_boundary_between(tmp_path / "a", tmp_path / "b") is None

    def test_a_destination_that_does_not_exist_yet_takes_its_nearest_existing_ancestor(self, tmp_path: Path) -> None:
        src, dst_root = tmp_path / "bind-a", tmp_path / "bind-b"
        src.mkdir()
        dst_root.mkdir()

        with _pinned_table(tmp_path, Path("/"), src, dst_root):
            assert mount_boundary_between(src / "wt", dst_root / "branch" / "repo") == (src, dst_root)

    def test_a_relative_path_is_anchored_on_the_cwd(self, tmp_path: Path) -> None:
        with _pinned_table(tmp_path, Path("/")):
            assert mount_boundary_between(Path(), Path("..")) is None

    def test_a_symlinked_ancestor_is_resolved_before_the_mount_lookup(self, tmp_path: Path) -> None:
        # mountinfo lists the KERNEL (real) path, never a symlinked alias — so
        # `_nearest_existing` must `.resolve()` the existing ancestor it finds, not
        # just return it. A mutation that drops `.resolve()` entirely (returning the
        # raw, unresolved path) reports NO mount at all here, because the table only
        # names the real path and a plain-prefix match against the symlinked one
        # never matches it.
        real_root = tmp_path / "real-root"
        real_root.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_root)

        with _pinned_table(tmp_path, real_root):
            assert mount_point_for(link / "branch" / "repo") == real_root

    def test_a_path_no_mount_point_covers_is_unknown(self, tmp_path: Path) -> None:
        # A table with no "/" row covers nothing here — unknown, never "same mount".
        with _pinned_table(tmp_path, tmp_path / "elsewhere"):
            assert mount_point_for(tmp_path) is None
            assert mount_boundary_between(tmp_path, tmp_path) is None
