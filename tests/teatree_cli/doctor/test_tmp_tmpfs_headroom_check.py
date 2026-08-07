"""``_check_tmp_tmpfs_headroom`` + ``_check_tmp_tmpfs_sizing`` — the `t3 doctor` RAM-tmpfs guards.

The box's ``/tmp`` is a small RAM tmpfs; agent/pytest/uv scratch can fill it to
ENOSPC and wedge the box. This surfaces the pressure as a WARN before it wedges,
but ONLY when ``/tmp`` is actually tmpfs (a disk-backed ``/tmp`` is skipped). It is
surfacing-only — always returns ``True`` (never gates the doctor exit code).

The sizing guard is the sibling: it measures how big the tmpfs may GET rather than
how full it is, which is the standing defect a fill-percent alarm cannot express.

The mount table and ``statvfs`` are stubbed so the tmpfs/threshold branches are
exercised deterministically, independent of the host's real ``/tmp``.
"""

import os
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor import checks_resources
from teatree.cli.doctor.checks_resources import (
    _check_tmp_tmpfs_headroom,
    _check_tmp_tmpfs_sizing,
    _tmp_mount_fstype,
    _tmpfs_sizing_target,
    _tmpfs_warn_percent,
)


def _mounts(tmp_path: Path, fstype: str, mount_point: str = "/tmp") -> Path:
    path = tmp_path / "mounts"
    path.write_text(
        f"/dev/root / ext4 rw 0 0\ntmpfs-or-disk {mount_point} {fstype} rw,nosuid 0 0\n",
        encoding="utf-8",
    )
    return path


class _FakeStatvfs:
    """A ``statvfs`` result modelling a temp fs at *used_pct* of *total* bytes."""

    def __init__(self, *, total: int, used_pct: int) -> None:
        self.f_frsize = 1
        self.f_blocks = total
        self.f_bavail = total - total * used_pct // 100


class TestTmpMountFstype:
    def test_returns_fstype_for_mount_point(self) -> None:
        text = "/dev/root / ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n"
        assert _tmp_mount_fstype(text, "/tmp") == "tmpfs"

    def test_last_matching_mount_wins(self) -> None:
        text = "a /tmp ext4 rw 0 0\nb /tmp tmpfs rw 0 0\n"
        assert _tmp_mount_fstype(text, "/tmp") == "tmpfs"

    def test_unmounted_point_is_none(self) -> None:
        assert _tmp_mount_fstype("/dev/root / ext4 rw 0 0\n", "/tmp") is None


class TestTmpfsWarnPercent:
    def test_default_when_unset(self) -> None:
        assert _tmpfs_warn_percent(None) == 80

    def test_parses_a_valid_override(self) -> None:
        assert _tmpfs_warn_percent("55") == 55

    def test_garbage_falls_back_to_default(self) -> None:
        assert _tmpfs_warn_percent("not-a-number") == 80

    def test_out_of_range_falls_back_to_default(self) -> None:
        assert _tmpfs_warn_percent("0") == 80
        assert _tmpfs_warn_percent("101") == 80


class TestTmpfsHeadroomCheck:
    def test_warns_when_tmpfs_over_threshold(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=1000, used_pct=95)):
            assert _check_tmp_tmpfs_headroom(mounts_path=mounts) is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "95% used" in out

    def test_silent_when_tmpfs_under_threshold(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=1000, used_pct=10)):
            assert _check_tmp_tmpfs_headroom(mounts_path=mounts) is True
        assert capsys.readouterr().out == ""

    def test_disk_backed_tmp_is_skipped(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "ext4")
        # Even a "full" disk /tmp is not the tmpfs wedge — no probe, no warning.
        with patch.object(os, "statvfs", side_effect=AssertionError("statvfs must not be called")):
            assert _check_tmp_tmpfs_headroom(mounts_path=mounts) is True
        assert capsys.readouterr().out == ""

    def test_threshold_override_is_honored(self, tmp_path: Path, capsys, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_TMPFS_WARN_PERCENT", "50")
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=1000, used_pct=60)):
            assert _check_tmp_tmpfs_headroom(mounts_path=mounts) is True
        assert "WARN" in capsys.readouterr().out

    def test_absent_mounts_file_is_silent_pass(self, tmp_path: Path, capsys) -> None:
        assert _check_tmp_tmpfs_headroom(mounts_path=tmp_path / "absent") is True
        assert capsys.readouterr().out == ""


class TestTmpfsSizingCheck:
    """``_check_tmp_tmpfs_sizing`` — how big the tmpfs may GET, not how full it is (#4165)."""

    def test_warns_when_the_tmpfs_may_claim_a_large_share_of_ram(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        # The measured box: a 15 GB /tmp on 31 GB of RAM.
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=15 * 1024**3, used_pct=1)):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "48% >= 25%" in out
        assert "size=4G" in out

    def test_the_remediation_keeps_it_a_tmpfs_rather_than_moving_it_to_disk(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=15 * 1024**3, used_pct=1)):
            _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024)
        out = capsys.readouterr().out
        assert "Keep it a tmpfs" in out
        assert "retention scratch --apply" in out

    def test_silent_when_the_tmpfs_is_capped_well_under_the_share(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=4 * 1024**3, used_pct=99)):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert capsys.readouterr().out == ""

    def test_disk_backed_tmp_is_never_a_sizing_finding(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "ext4")
        with patch.object(os, "statvfs", side_effect=AssertionError("statvfs must not be called")):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert capsys.readouterr().out == ""

    def test_threshold_override_is_honored(self, tmp_path: Path, capsys, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_TMPFS_MAX_RAM_PERCENT", "5")
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=4 * 1024**3, used_pct=1)):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert "WARN" in capsys.readouterr().out

    def test_absent_mounts_file_is_a_silent_pass(self, tmp_path: Path, capsys) -> None:
        assert _check_tmp_tmpfs_sizing(mounts_path=tmp_path / "absent") is True
        assert capsys.readouterr().out == ""

    def test_a_failing_statvfs_is_a_silent_pass(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", side_effect=OSError("no such mount")):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert capsys.readouterr().out == ""

    def test_a_zero_sized_tmpfs_is_a_silent_pass(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=0, used_pct=0)):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert capsys.readouterr().out == ""

    def test_an_unreadable_ram_total_is_a_silent_pass(self, tmp_path: Path, capsys) -> None:
        mounts = _mounts(tmp_path, "tmpfs")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total=15 * 1024**3, used_pct=1)):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=0) is True
        assert capsys.readouterr().out == ""


class TestTmpfsSizingTarget:
    """Which path the sizing check inspects (#4165 review finding).

    ``t3 doctor check`` runs INSIDE the container, where a hard-coded ``/tmp``
    names the image's own overlay layer, never the host's tmpfs — so the default
    resolution must prefer the ``/host-tmp`` bind (the scratch sweep's own mount)
    when it exists, and only fall back to ``/tmp`` when it does not.
    """

    def test_an_explicit_tmp_dir_always_wins(self, tmp_path: Path) -> None:
        with patch.object(checks_resources, "_HOST_TMP_MOUNT", tmp_path):
            assert _tmpfs_sizing_target("/custom") == "/custom"

    def test_prefers_the_host_bind_when_it_is_mounted(self, tmp_path: Path) -> None:
        with patch.object(checks_resources, "_HOST_TMP_MOUNT", tmp_path):
            assert _tmpfs_sizing_target(None) == str(tmp_path)

    def test_falls_back_to_tmp_when_the_host_bind_is_absent(self, tmp_path: Path) -> None:
        with patch.object(checks_resources, "_HOST_TMP_MOUNT", tmp_path / "absent"):
            assert _tmpfs_sizing_target(None) == "/tmp"


class TestTmpfsSizingCheckPrefersTheHostBind:
    """End-to-end: the check itself follows the resolved target, not a literal /tmp.

    Regression for the review finding: reverting the default from ``None`` back to
    a hard-coded ``"/tmp"`` makes ``test_reports_the_host_tmpfs_by_its_bind_path``
    go silent, because the mounts file here lists ONLY ``/host-tmp`` as tmpfs — the
    exact shape of "doctor check runs in the container, where /tmp is not a
    distinct mount".
    """

    def test_reports_the_host_tmpfs_by_its_bind_path(self, tmp_path: Path, capsys) -> None:
        host_tmp = tmp_path / "host-tmp"
        host_tmp.mkdir()
        # The container's own /proc/mounts spells the bind mount at its resolved
        # path (a bind mount reports the SOURCE's fstype at the new mountpoint) —
        # not the literal /host-tmp target string.
        mounts = _mounts(tmp_path, "tmpfs", mount_point=str(host_tmp))

        with (
            patch.object(checks_resources, "_HOST_TMP_MOUNT", host_tmp),
            patch.object(os, "statvfs", return_value=_FakeStatvfs(total=15 * 1024**3, used_pct=1)),
        ):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True

        out = capsys.readouterr().out
        assert "WARN" in out
        assert str(host_tmp) in out

    def test_silent_when_the_host_bind_is_absent_and_this_venues_own_tmp_is_not_tmpfs(
        self, tmp_path: Path, capsys
    ) -> None:
        mounts = _mounts(tmp_path, "ext4")  # this venue's own /tmp, disk-backed

        with (
            patch.object(checks_resources, "_HOST_TMP_MOUNT", tmp_path / "absent"),
            patch.object(os, "statvfs", side_effect=AssertionError("statvfs must not be called")),
        ):
            assert _check_tmp_tmpfs_sizing(mounts_path=mounts, total_ram_mib=31 * 1024) is True
        assert capsys.readouterr().out == ""
