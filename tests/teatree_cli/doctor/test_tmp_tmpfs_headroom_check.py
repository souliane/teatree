"""``_check_tmp_tmpfs_headroom`` — the `t3 doctor` RAM-tmpfs-fill guard.

The box's ``/tmp`` is a small RAM tmpfs; agent/pytest/uv scratch can fill it to
ENOSPC and wedge the box. This surfaces the pressure as a WARN before it wedges,
but ONLY when ``/tmp`` is actually tmpfs (a disk-backed ``/tmp`` is skipped). It is
surfacing-only — always returns ``True`` (never gates the doctor exit code).

The mount table and ``statvfs`` are stubbed so the tmpfs/threshold branches are
exercised deterministically, independent of the host's real ``/tmp``.
"""

import os
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor.checks_environment import _check_tmp_tmpfs_headroom, _tmp_mount_fstype, _tmpfs_warn_percent


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
