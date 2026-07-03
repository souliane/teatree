"""Unit tests for the shared RAM probe + nCPU concurrency default."""

from unittest.mock import patch

import pytest

from teatree.utils.ram_probe import (
    _linux_ram_used_percent,
    _macos_ram_used_percent,
    default_provision_concurrency,
    read_ram_used_percent,
)


def test_read_ram_used_percent_dispatches_darwin() -> None:
    with (
        patch("teatree.utils.ram_probe.platform.system", return_value="Darwin"),
        patch("teatree.utils.ram_probe._macos_ram_used_percent", return_value=42.0) as macos,
    ):
        assert read_ram_used_percent() == pytest.approx(42.0)
    macos.assert_called_once()


def test_read_ram_used_percent_dispatches_linux() -> None:
    with (
        patch("teatree.utils.ram_probe.platform.system", return_value="Linux"),
        patch("teatree.utils.ram_probe._linux_ram_used_percent", return_value=13.0) as linux,
    ):
        assert read_ram_used_percent() == pytest.approx(13.0)
    linux.assert_called_once()


def test_read_ram_used_percent_unknown_platform_returns_zero() -> None:
    with patch("teatree.utils.ram_probe.platform.system", return_value="FreeBSD"):
        assert read_ram_used_percent() == pytest.approx(0.0)


def test_macos_probe_no_binaries_returns_zero() -> None:
    with patch("shutil.which", return_value=None):
        assert _macos_ram_used_percent() == pytest.approx(0.0)


def test_linux_probe_missing_proc_meminfo_returns_zero() -> None:
    with patch("builtins.open", side_effect=OSError):
        assert _linux_ram_used_percent() == pytest.approx(0.0)


def test_linux_probe_parses_meminfo(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
    with patch("builtins.open", lambda *_a, **_k: meminfo.open(encoding="utf-8")):
        used = _linux_ram_used_percent()
    assert used == pytest.approx(75.0)


def test_default_provision_concurrency_halves_cpu_count() -> None:
    assert default_provision_concurrency(cpu_count=8) == 4


def test_default_provision_concurrency_floors_at_one() -> None:
    assert default_provision_concurrency(cpu_count=1) == 1
    assert default_provision_concurrency(cpu_count=0) == 1


def test_default_provision_concurrency_reads_os_cpu_count_when_unset() -> None:
    with patch("teatree.utils.ram_probe.os.cpu_count", return_value=6):
        assert default_provision_concurrency() == 3
