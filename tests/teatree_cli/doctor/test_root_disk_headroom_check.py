"""``_check_root_disk_headroom`` — the `t3 doctor` root-filesystem fill guard (#3852).

The gap this closes: the resource probes covered the ``/tmp`` tmpfs and the
container memory cap, and nothing measured the root filesystem — so a box that
climbed to 96% and then 97% full reported a clean doctor the whole way. Unlike its
siblings this one is PERCENT-aware, because a fixed free-GB floor says nothing on
its own: 10 GB free is comfortable on a 100 GB disk and terminal on a 2 TB one.

``statvfs`` is stubbed so each band is exercised independently of the host's real
root filesystem.
"""

import os
from unittest.mock import patch

from teatree.cli.doctor.checks_resources import _check_root_disk_headroom, _disk_percent_threshold


class _FakeStatvfs:
    """A ``statvfs`` result modelling a filesystem at *used_pct* of *total_gib*."""

    def __init__(self, *, total_gib: int, used_pct: int) -> None:
        self.f_frsize = 1024**3
        self.f_blocks = total_gib
        self.f_bavail = total_gib - total_gib * used_pct // 100


class TestDiskPercentThreshold:
    def test_default_when_unset(self) -> None:
        assert _disk_percent_threshold(None, default=85) == 85

    def test_parses_a_valid_override(self) -> None:
        assert _disk_percent_threshold("70", default=85) == 70

    def test_garbage_falls_back_to_default(self) -> None:
        assert _disk_percent_threshold("not-a-number", default=85) == 85

    def test_out_of_range_falls_back_to_default(self) -> None:
        assert _disk_percent_threshold("0", default=85) == 85
        assert _disk_percent_threshold("101", default=85) == 85


class TestRootDiskHeadroomCheck:
    def test_fails_at_the_critical_band(self, capsys) -> None:
        """The band the host sat in unnoticed while every probe reported green."""
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=235, used_pct=96)):
            assert _check_root_disk_headroom() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "96% used" in out
        assert "reclaim-disk" in out

    def test_warns_at_the_warn_band(self, capsys) -> None:
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=235, used_pct=88)):
            assert _check_root_disk_headroom() is True
        out = capsys.readouterr().out
        assert "WARN" in out
        assert "88% used" in out

    def test_silent_with_headroom(self, capsys) -> None:
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=235, used_pct=40)):
            assert _check_root_disk_headroom() is True
        assert capsys.readouterr().out == ""

    def test_a_small_disk_at_the_same_percent_still_fires(self, capsys) -> None:
        """Percent, not free-GB: 4 GB free on a 100 GB disk is the same trouble as 9 GB on 235 GB.

        Under the absolute-GB thresholds this case reads as fine (4 GB free clears
        no band it is compared against), which is exactly why the alarm is
        percent-shaped.
        """
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=100, used_pct=96)):
            assert _check_root_disk_headroom() is False
        assert "FAIL" in capsys.readouterr().out

    def test_thresholds_are_overridable(self, capsys, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_DISK_WARN_PERCENT", "30")
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=235, used_pct=40)):
            assert _check_root_disk_headroom() is True
        assert "WARN" in capsys.readouterr().out

    def test_an_unreadable_filesystem_is_a_silent_pass(self, capsys) -> None:
        """Crash-proof, like its siblings — a diagnostic never aborts the doctor run."""
        with patch.object(os, "statvfs", side_effect=OSError("nope")):
            assert _check_root_disk_headroom() is True
        assert capsys.readouterr().out == ""

    def test_a_zero_sized_filesystem_is_a_silent_pass(self, capsys) -> None:
        with patch.object(os, "statvfs", return_value=_FakeStatvfs(total_gib=0, used_pct=0)):
            assert _check_root_disk_headroom() is True
        assert capsys.readouterr().out == ""
