"""Unit tests for the shared RAM probe + nCPU/RAM concurrency and worker-sizing derivations."""

import subprocess
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.utils import ram_probe
from teatree.utils.ram_probe import (
    _cgroup_v2_cpu_quota,
    _emit_compose_sizing,
    _linux_ram_used_percent,
    _macos_ram_used_percent,
    available_cpu_count,
    default_provision_concurrency,
    derive_worker_cpus,
    derive_worker_mem_limit_mib,
    host_total_ram_mib,
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


class TestMacosPageSize:
    """The mac probe must read the page size off ``vm_stat``, never assume 4 KiB.

    Apple Silicon reports 16384-byte pages. Assuming 4096 shrinks the
    reclaimable total 4x, which reads back as a near-full host and makes
    ``check_provision_admission`` hold every provision on a host with GBs free.
    """

    # A real arm64 capture (24 GiB host): free + inactive = 470274 reclaimable pages.
    _TOTAL_BYTES = 25769803776
    _RECLAIMABLE_PAGES = 83371 + 386903
    _VM_STAT_16K = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    83371.
Pages active:                                  397317.
Pages inactive:                               386903.
Pages speculative:                               9228.
Pages wired down:                              383587.
"""

    def _probe(self, vm_stat_output: str) -> float:
        """Run the mac probe against *vm_stat_output* with a fixed ``hw.memsize``."""

        def fake_run(cmd, **_kwargs):
            stdout = str(self._TOTAL_BYTES) if "hw.memsize" in cmd else vm_stat_output
            return CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        with (
            patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("teatree.utils.run.run_checked", side_effect=fake_run),
        ):
            return _macos_ram_used_percent()

    def _expected_percent(self, page_size: int) -> float:
        used = self._TOTAL_BYTES - self._RECLAIMABLE_PAGES * page_size
        return used * 100.0 / self._TOTAL_BYTES

    def test_uses_the_page_size_from_the_vm_stat_header(self) -> None:
        assert self._probe(self._VM_STAT_16K) == pytest.approx(self._expected_percent(16384))

    def test_does_not_fall_back_to_a_hardcoded_four_kib_page(self) -> None:
        # The 4 KiB reading of this same capture is ~92.5% — a phantom that
        # trips the 85% provision-admission ceiling on a host with ~7 GB free.
        assert self._probe(self._VM_STAT_16K) != pytest.approx(self._expected_percent(4096))

    def test_unparsable_header_falls_back_to_four_kib(self) -> None:
        # "Can't tell" keeps the historical 4 KiB arithmetic rather than raising.
        headerless = self._VM_STAT_16K.replace("(page size of 16384 bytes)", "(page size unknown)")
        assert self._probe(headerless) == pytest.approx(self._expected_percent(4096))


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


def test_default_provision_concurrency_reads_available_cpu_when_unset() -> None:
    with patch("teatree.utils.ram_probe.available_cpu_count", return_value=6):
        assert default_provision_concurrency() == 3


def test_available_cpu_count_takes_the_minimum_signal() -> None:
    # A host with 8 physical cores but a 2-core cgroup quota must derive from 2.
    with (
        patch("os.process_cpu_count", return_value=8),
        patch("teatree.utils.ram_probe.os.cpu_count", return_value=8),
        patch("teatree.utils.ram_probe._cgroup_v2_cpu_quota", return_value=2),
    ):
        assert available_cpu_count() == 2


def test_available_cpu_count_ignores_absent_cgroup_cap() -> None:
    with (
        patch("os.process_cpu_count", return_value=4),
        patch("teatree.utils.ram_probe.os.cpu_count", return_value=4),
        patch("teatree.utils.ram_probe._cgroup_v2_cpu_quota", return_value=None),
    ):
        assert available_cpu_count() == 4


def test_available_cpu_count_floors_at_one() -> None:
    with (
        patch("os.process_cpu_count", return_value=None),
        patch("teatree.utils.ram_probe.os.cpu_count", return_value=None),
        patch("teatree.utils.ram_probe._cgroup_v2_cpu_quota", return_value=None),
    ):
        assert available_cpu_count() == 1


def test_cgroup_v2_cpu_quota_parses_capped() -> None:
    with patch("pathlib.Path.read_text", return_value="150000 100000\n"):  # 1.5 cores → ceil 2
        assert _cgroup_v2_cpu_quota() == 2


def test_cgroup_v2_cpu_quota_unlimited_is_none() -> None:
    with patch("pathlib.Path.read_text", return_value="max 100000\n"):
        assert _cgroup_v2_cpu_quota() is None


def test_cgroup_v2_cpu_quota_missing_file_is_none() -> None:
    with patch("pathlib.Path.read_text", side_effect=OSError):
        assert _cgroup_v2_cpu_quota() is None


class TestHostTotalRamMib:
    def test_linux_reads_memtotal(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Linux"),
            patch("teatree.utils.ram_probe._linux_meminfo", return_value={"MemTotal": 33554432}),
        ):
            # 33554432 kB / 1024 = 32768 MiB.
            assert host_total_ram_mib() == 32768

    def test_linux_unreadable_is_zero(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Linux"),
            patch("teatree.utils.ram_probe._linux_meminfo", return_value={}),
        ):
            assert host_total_ram_mib() == 0

    def test_unknown_platform_is_zero(self) -> None:
        with patch("teatree.utils.ram_probe.platform.system", return_value="FreeBSD"):
            assert host_total_ram_mib() == 0


class TestDeriveWorkerCpus:
    def test_reserves_one_core_for_sidecars(self) -> None:
        assert derive_worker_cpus(cpu_count=8) == 7

    def test_floors_at_one(self) -> None:
        assert derive_worker_cpus(cpu_count=1) == 1
        assert derive_worker_cpus(cpu_count=0) == 1

    def test_reads_available_cpu_when_unset(self) -> None:
        with patch("teatree.utils.ram_probe.available_cpu_count", return_value=4):
            assert derive_worker_cpus() == 3


class TestDeriveWorkerMemLimitMib:
    def test_reserves_siblings_and_headroom(self) -> None:
        # (32000 - 1280) * 0.8 = 24576.
        assert derive_worker_mem_limit_mib(total_ram_mib=32000) == 24576

    def test_floors_at_two_gib(self) -> None:
        # A tiny host still gets a usable worker cap, even above its RAM (a cap only).
        assert derive_worker_mem_limit_mib(total_ram_mib=1000) == 2048

    def test_unreadable_ram_returns_zero_so_default_holds(self) -> None:
        # 0 signals deploy.sh to keep the compose default rather than cap blindly.
        assert derive_worker_mem_limit_mib(total_ram_mib=0) == 0

    def test_reads_host_total_when_unset(self) -> None:
        with patch("teatree.utils.ram_probe.host_total_ram_mib", return_value=32000):
            assert derive_worker_mem_limit_mib() == 24576


class TestComposeSizingMain:
    """The standalone `python3 ram_probe.py compose-sizing` deploy.sh consumes."""

    def test_emits_shell_assignments(self, capsys) -> None:
        with (
            patch("teatree.utils.ram_probe.derive_worker_cpus", return_value=7),
            patch("teatree.utils.ram_probe.derive_worker_mem_limit_mib", return_value=24576),
        ):
            _emit_compose_sizing()
        out = capsys.readouterr().out
        assert "TEATREE_WORKER_CPUS=7" in out
        assert "TEATREE_WORKER_MEM_LIMIT=24576m" in out

    def test_omits_mem_when_ram_unreadable(self, capsys) -> None:
        with (
            patch("teatree.utils.ram_probe.derive_worker_cpus", return_value=3),
            patch("teatree.utils.ram_probe.derive_worker_mem_limit_mib", return_value=0),
        ):
            _emit_compose_sizing()
        out = capsys.readouterr().out
        assert "TEATREE_WORKER_CPUS=3" in out
        assert "TEATREE_WORKER_MEM_LIMIT" not in out

    def test_runs_as_a_module_without_importing_teatree(self) -> None:
        # deploy.sh invokes `python3 <file> compose-sizing` on the host with no
        # teatree on sys.path — the module must run pure-stdlib on that path.
        module = Path(ram_probe.__file__)
        proc = subprocess.run(
            [sys.executable, str(module), "compose-sizing"],
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            check=True,
        )
        assert "TEATREE_WORKER_CPUS=" in proc.stdout
