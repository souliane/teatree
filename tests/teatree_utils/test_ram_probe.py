"""Unit tests for the shared RAM probe + nCPU/RAM concurrency and worker-sizing derivations."""

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import ClassVar
from unittest.mock import patch

import pytest

from teatree.core.admission_pressure import RAM_RESUME_FLOOR_GB, resume_ceiling_conflict
from teatree.utils import ram_probe
from teatree.utils.ram_probe import (
    DockerWorkerSizing,
    _cgroup_v2_cpu_quota,
    _linux_ram_used_percent,
    _macos_ram_used_percent,
    available_cpu_count,
    cgroup_v2_memory_mib,
    default_provision_concurrency,
    host_available_ram_mib,
    host_total_ram_mib,
    linux_mem_available_kb,
    read_ram_used_percent,
)
from teatree.utils.ram_scope import DEFAULT_AGENT_WORKLOAD_FLOOR_GIB, RamHeadroom


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
    with (
        patch("teatree.utils.ram_probe.cgroup_file", return_value=Path("/sys/fs/cgroup/cpu.max")),
        patch("pathlib.Path.read_text", return_value="150000 100000\n"),
    ):  # 1.5 cores → ceil 2
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
        assert DockerWorkerSizing.worker_cpus(cpu_count=8, daemon_cpus=0) == 7

    def test_floors_at_one(self) -> None:
        assert DockerWorkerSizing.worker_cpus(cpu_count=1, daemon_cpus=0) == 1
        assert DockerWorkerSizing.worker_cpus(cpu_count=0, daemon_cpus=0) == 1
        assert DockerWorkerSizing.worker_cpus(cpu_count=8, daemon_cpus=1) == 1

    def test_reads_available_cpu_when_unset(self) -> None:
        with (
            patch("teatree.utils.ram_probe.available_cpu_count", return_value=4),
            patch("teatree.utils.ram_probe.DockerWorkerSizing.daemon_cpu_count", return_value=0),
        ):
            assert DockerWorkerSizing.worker_cpus() == 3


class TestWorkerCpusNeverExceedTheDockerEngine:
    """Docker Desktop's VM can hold fewer CPUs than the host; the daemon rejects a larger ``cpus``."""

    @pytest.mark.parametrize(
        ("host", "engine", "expected"),
        [
            pytest.param(10, 8, 7, id="docker-desktop-vm-smaller-than-host"),
            pytest.param(4, 8, 3, id="host-smaller-than-engine"),
            pytest.param(8, 8, 7, id="linux-engine-equals-host"),
            pytest.param(10, 0, 9, id="unreadable-engine-keeps-host"),
        ],
    )
    def test_derives_from_the_smaller_of_host_and_engine(self, host: int, engine: int, expected: int) -> None:
        assert DockerWorkerSizing.worker_cpus(cpu_count=host, daemon_cpus=engine) == expected

    def test_probes_the_engine_by_default(self) -> None:
        with (
            patch("teatree.utils.ram_probe.available_cpu_count", return_value=10),
            patch("teatree.utils.ram_probe.DockerWorkerSizing.daemon_cpu_count", return_value=8),
        ):
            assert DockerWorkerSizing.worker_cpus() == 7


class TestDockerDaemonCpuCount:
    def test_parses_ncpu(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/docker"),
            patch(
                "subprocess.run",
                return_value=CompletedProcess(args=[], returncode=0, stdout="8\n", stderr=""),
            ) as run,
        ):
            assert DockerWorkerSizing.daemon_cpu_count() == 8
        assert "{{.NCPU}}" in run.call_args.args[0]

    def test_missing_docker_reads_zero(self) -> None:
        with patch("shutil.which", return_value=None):
            assert DockerWorkerSizing.daemon_cpu_count() == 0

    def test_unreachable_daemon_reads_zero(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/docker"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "docker")),
        ):
            assert DockerWorkerSizing.daemon_cpu_count() == 0


class TestDeriveWorkerMemLimitMib:
    def test_reserves_siblings_and_headroom(self) -> None:
        # (32000 - 3328) * 0.8 = 22937.
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=32000, daemon_ram_mib=0) == 22937

    def test_a_tiny_host_has_no_workable_cap_and_says_so(self) -> None:
        # Was a 2 GiB floor. A 2 GiB cap is HALF the floor `t3 doctor check` hard-FAILs as
        # a broken product, so "a usable worker cap" it was not — the sizer now refuses
        # rather than emitting one the product itself calls broken (#151).
        sizing = DockerWorkerSizing.worker_sizing(total_ram_mib=1000, daemon_ram_mib=0)

        assert sizing.mem_limit_mib == 0
        assert sizing.refusal

    def test_unreadable_ram_returns_zero_so_default_holds(self) -> None:
        # 0 signals deploy.sh to keep the compose default rather than cap blindly.
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=0, daemon_ram_mib=0) == 0

    def test_reads_host_total_when_unset(self) -> None:
        with (
            patch("teatree.utils.ram_probe.host_total_ram_mib", return_value=32000),
            patch("teatree.utils.ram_probe.DockerWorkerSizing.daemon_total_ram_mib", return_value=0),
        ):
            assert DockerWorkerSizing.worker_mem_limit_mib() == 22937


class TestWorkerCapNeverExceedsTheDockerVm:
    """Under Docker Desktop the daemon's VM, not host RAM, is the real ceiling.

    A cap derived from a 24 GiB host but applied inside an 8 GiB VM can never
    bind: the cgroup limit sits above the whole machine, so memory pressure
    resolves as a GLOBAL oom-kill (``constraint=CONSTRAINT_NONE``) that reaps the
    worker mid-run and leaves dockerd without an exit event — the container then
    refuses `stop` and exits 137.
    """

    def test_daemon_vm_smaller_than_host_wins(self) -> None:
        # 24 GiB host, 12 GiB Docker Desktop VM: (12288 - 3328) * 0.8 = 7168, and the
        # 24 GiB host would have derived 16998 — a cap the VM could never hold.
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=24576, daemon_ram_mib=12288) == 7168

    def test_a_daemon_vm_too_small_for_a_workable_cap_refuses_rather_than_using_the_host(self) -> None:
        # The same property at a VM below the fit threshold: the host's roomier figure
        # must not rescue it, because the cap would land where the brake never releases.
        sizing = DockerWorkerSizing.worker_sizing(total_ram_mib=24576, daemon_ram_mib=8192)

        assert sizing.mem_limit_mib == 0
        assert sizing.refusal
        assert "8192" in sizing.refusal

    def test_host_wins_when_it_is_the_smaller_of_the_two(self) -> None:
        # Linux box: the daemon shares the host kernel, so neither figure inflates.
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=32000, daemon_ram_mib=32000) == 22937

    def test_unreadable_daemon_falls_back_to_host(self) -> None:
        # No docker binary / unreachable daemon must not zero out a good host read.
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=32000, daemon_ram_mib=0) == 22937

    def test_daemon_alone_still_sizes_when_host_is_unreadable(self) -> None:
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=0, daemon_ram_mib=12288) == 7168

    def test_probes_the_daemon_by_default(self) -> None:
        with (
            patch("teatree.utils.ram_probe.host_total_ram_mib", return_value=24576),
            patch("teatree.utils.ram_probe.DockerWorkerSizing.daemon_total_ram_mib", return_value=12288),
        ):
            assert DockerWorkerSizing.worker_mem_limit_mib() == 7168


class TestDockerDaemonTotalRamMib:
    def test_parses_mem_total_bytes(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/docker"),
            patch(
                "subprocess.run",
                return_value=CompletedProcess(args=[], returncode=0, stdout="8322101248\n", stderr=""),
            ),
        ):
            assert ram_probe.DockerWorkerSizing.daemon_total_ram_mib() == 7936

    def test_missing_docker_reads_zero(self) -> None:
        with patch("shutil.which", return_value=None):
            assert ram_probe.DockerWorkerSizing.daemon_total_ram_mib() == 0

    def test_unreachable_daemon_reads_zero(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/docker"),
            patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "docker")),
        ):
            assert ram_probe.DockerWorkerSizing.daemon_total_ram_mib() == 0

    def test_unparsable_output_reads_zero(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/local/bin/docker"),
            patch(
                "subprocess.run",
                return_value=CompletedProcess(args=[], returncode=0, stdout="<nil>\n", stderr=""),
            ),
        ):
            assert ram_probe.DockerWorkerSizing.daemon_total_ram_mib() == 0


class TestComposeSizingMain:
    """The standalone `python3 ram_probe.py compose-sizing` deploy.sh consumes."""

    def test_runs_as_a_module_without_importing_teatree(self) -> None:
        # deploy.sh invokes `python3 <file> compose-sizing` on the host with no
        # teatree on sys.path — the module must run pure-stdlib on that path.
        # Exit 3 is a legitimate outcome here (this machine's daemon may be too small to
        # host a workable cap), so the assertion is on the pure-stdlib run, never on the
        # runner's RAM: `check=True` would make the test fail on a small CI box.
        proc = self._run_compose_sizing({})
        assert proc.returncode in {0, 3}, proc.stderr
        assert "Traceback" not in proc.stderr
        assert "TEATREE_WORKER_CPUS=" in proc.stdout

    def test_an_operator_exported_cpu_cap_is_not_overwritten(self) -> None:
        # deploy.sh evals this output after reading the operator's value, so emitting
        # a derived line would silently replace it.
        proc = self._run_compose_sizing({"TEATREE_WORKER_CPUS": "5"})

        assert proc.returncode in {0, 3}, proc.stderr
        assert "TEATREE_WORKER_CPUS=" not in proc.stdout

    @staticmethod
    def _run_compose_sizing(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(Path(ram_probe.__file__)), "compose-sizing"],
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            env={**{k: v for k, v in os.environ.items() if k != "TEATREE_WORKER_CPUS"}, **extra_env},
            check=False,
        )


class TestHostAvailableRamMib:
    def test_linux_reads_memavailable(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Linux"),
            patch("teatree.utils.ram_probe._linux_meminfo", return_value={"MemAvailable": 20971520}),
        ):
            assert host_available_ram_mib() == 20480

    def test_linux_unreadable_is_zero(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Linux"),
            patch("teatree.utils.ram_probe._linux_meminfo", return_value={}),
        ):
            assert host_available_ram_mib() == 0

    def test_macos_sums_the_reclaimable_page_classes(self) -> None:
        stat = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 32768.\nPages inactive: 32768.\n"
        )
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Darwin"),
            patch("shutil.which", return_value="/usr/bin/vm_stat"),
            patch("teatree.utils.run.run_checked", return_value=CompletedProcess([], 0, stat, "")),
        ):
            # (32768 + 32768) pages * 16 KiB = 1024 MiB.
            assert host_available_ram_mib() == 1024

    def test_macos_without_vm_stat_is_zero(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Darwin"),
            patch("shutil.which", return_value=None),
        ):
            assert host_available_ram_mib() == 0

    def test_macos_failing_vm_stat_is_zero(self) -> None:
        with (
            patch("teatree.utils.ram_probe.platform.system", return_value="Darwin"),
            patch("shutil.which", return_value="/usr/bin/vm_stat"),
            patch("teatree.utils.run.run_checked", side_effect=OSError),
        ):
            assert host_available_ram_mib() == 0

    def test_unknown_platform_is_zero(self) -> None:
        with patch("teatree.utils.ram_probe.platform.system", return_value="FreeBSD"):
            assert host_available_ram_mib() == 0


class TestLinuxMemAvailableKb:
    """#4104 — a pressure ladder must tell "unreadable" apart from "no memory left".

    :func:`host_available_ram_mib` collapses both to ``0``, which reads as CRITICAL
    to a threshold comparison. This reader keeps them distinct.
    """

    def _meminfo(self, tmp_path: Path, body: str) -> str:
        path = tmp_path / "meminfo"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_reads_mem_available_from_a_real_file(self, tmp_path: Path) -> None:
        body = "MemTotal:       31457280 kB\nMemFree:          204800 kB\nMemAvailable:    1048576 kB\n"
        assert linux_mem_available_kb(self._meminfo(tmp_path, body)) == 1048576

    def test_zero_available_is_a_reading_not_a_failure(self, tmp_path: Path) -> None:
        assert linux_mem_available_kb(self._meminfo(tmp_path, "MemAvailable:          0 kB\n")) == 0

    def test_missing_key_is_none(self, tmp_path: Path) -> None:
        assert linux_mem_available_kb(self._meminfo(tmp_path, "MemTotal:       31457280 kB\n")) is None

    def test_unreadable_path_is_none(self) -> None:
        assert linux_mem_available_kb("/nonexistent/meminfo") is None


class TestCgroupV2MemoryMib:
    def test_unreadable_membership_never_substitutes_the_root_cgroup(self) -> None:
        def read_text(path: Path, **_kwargs: object) -> str:
            if str(path) == "/proc/self/cgroup":
                raise OSError
            if str(path) == "/sys/fs/cgroup/memory.max":
                return str(32 * 1024**3)
            raise FileNotFoundError(str(path))

        with patch("pathlib.Path.read_text", read_text):
            assert cgroup_v2_memory_mib("memory.max") is None

    def test_nested_cgroup_reads_its_own_cap_not_the_mount_root(self) -> None:
        def read_text(path: Path, **_kwargs: object) -> str:
            if str(path) == "/proc/self/cgroup":
                return "0::/system.slice/worker.scope\n"
            if str(path) == "/sys/fs/cgroup/system.slice/worker.scope/memory.max":
                return str(8 * 1024**3)
            raise FileNotFoundError(str(path))

        with patch("pathlib.Path.read_text", read_text):
            assert cgroup_v2_memory_mib("memory.max") == 8192

    def test_parses_a_byte_count(self) -> None:
        with (
            patch("teatree.utils.ram_probe.cgroup_file", return_value=Path("/sys/fs/cgroup/memory.max")),
            patch("pathlib.Path.read_text", return_value="17179869184\n"),
        ):
            assert cgroup_v2_memory_mib("memory.max") == 16384

    def test_unlimited_is_none(self) -> None:
        with patch("pathlib.Path.read_text", return_value="max\n"):
            assert cgroup_v2_memory_mib("memory.max") is None

    def test_missing_file_is_none(self) -> None:
        with patch("pathlib.Path.read_text", side_effect=OSError):
            assert cgroup_v2_memory_mib("memory.current") is None

    def test_unparsable_value_is_none(self) -> None:
        with patch("pathlib.Path.read_text", return_value="not-a-number\n"):
            assert cgroup_v2_memory_mib("memory.max") is None


class TestTheEmittedCapCanAlwaysReleaseTheBrake:
    """The deploy-time sizer must not emit a cap the admission governor can never release.

    A braked governor holds itself to ``RAM_RESUME_FLOOR_GB``, and cgroup headroom can
    never exceed the cap, so any cap at or under 6 GiB is unsatisfiable with the container
    completely EMPTY. Measured on the incident host 2026-09-04: ``docker inspect`` reported
    ``HostConfig.Memory = 5543821312`` for the worker, exactly ``int((9937 - 3328) * 0.8)``,
    and the governor refused every plan with "the cgroup memory cap is 5.16 GiB ... so once
    braked this lane can NEVER resume".

    Clamping DOWN under the agent-workload floor does not fix that; it changes which failure
    you get. Below the floor the cgroup stops being box-scoped, so ``box_watermark_mib``
    answers from the HOST component and work is sized against memory the container cannot
    hand out, while ``t3 doctor check``'s ``_check_worker_memory_cap`` hard-FAILs the same
    cap as a broken product. So the cap goes UP, or there is no workable cap and the sizer
    REFUSES.
    """

    MEASURED_INCIDENT_DAEMON_MIB: ClassVar[int] = 9937
    #: Daemon totals whose UNCLAMPED arithmetic lands at or under the resume floor.
    BAND_DAEMON_MIB: ClassVar[tuple[int, int]] = (8448, 11008)
    #: The smallest daemon total that can hold a floor-clearing cap beside the siblings.
    MIN_WORKABLE_DAEMON_MIB: ClassVar[int] = 9473

    @staticmethod
    def _sizing(daemon_mib: int) -> ram_probe.WorkerSizing:
        return DockerWorkerSizing.worker_sizing(total_ram_mib=0, daemon_ram_mib=daemon_mib)

    def test_the_restated_resume_floor_matches_its_owning_constant(self) -> None:
        """``ram_probe`` runs as a BARE script under ``deploy.sh``, so it cannot import it.

        Restated locally and pinned here — the vendored-authority pattern — so a change to
        the owning constant fails loudly instead of silently re-opening the trap. Only the
        RESUME floor is restated: the agent-workload floor is operator-overridable, and
        restating an overridable value is what made the two disagree (#151).
        """
        assert int(RAM_RESUME_FLOOR_GB * 1024) == ram_probe._RESUME_FLOOR_MIB

    def test_the_measured_incident_total_now_clears_the_resume_floor(self) -> None:
        sizing = self._sizing(self.MEASURED_INCIDENT_DAEMON_MIB)

        assert sizing.refusal is None
        assert sizing.mem_limit_mib > int(RAM_RESUME_FLOOR_GB * 1024)
        assert sizing.mem_limit_mib != 5286, "the 5.16 GiB cap that braked the factory for a whole session"

    def test_the_emitted_cap_clears_the_conflict_predicate_that_names_the_fault(self) -> None:
        assert resume_ceiling_conflict(self._sizing(self.MEASURED_INCIDENT_DAEMON_MIB).mem_limit_mib / 1024) is None

    def test_the_emitted_cap_is_box_scoped_so_the_cgroup_still_governs(self) -> None:
        """The direction that matters: a cap must not buy safety by ceasing to govern.

        This deliberately INVERTS the assertion the clamp-down arm carried, which read a
        cap falling OUT of box scope as the intent. Out of scope means ``box_watermark_mib``
        answers from the host component, so a freer host admits MORE work into a container
        that cannot hold it — unbounded, and invisible on every health surface.
        """
        cap = self._sizing(self.MEASURED_INCIDENT_DAEMON_MIB).mem_limit_mib

        assert RamHeadroom(available_mib=cap, cgroup_limit_mib=cap, host_available_mib=cap).cgroup_is_box_scoped

    def test_no_daemon_total_yields_a_cap_at_or_under_the_resume_floor(self) -> None:
        """The whole invariant, swept: every emitted cap can release the brake."""
        offenders = {
            daemon: sizing.mem_limit_mib
            for daemon in range(2048, 32769, 64)
            if 0 < (sizing := self._sizing(daemon)).mem_limit_mib <= int(RAM_RESUME_FLOOR_GB * 1024)
        }

        assert not offenders, f"caps at/under the resume floor for daemon totals: {sorted(offenders)[:8]}"

    def test_the_sweep_visits_totals_that_would_otherwise_offend(self) -> None:
        """Positive control: without the clamp the sweep above HAS offenders to find."""
        unclamped = [
            int((daemon - 3328) * 0.8) for daemon in range(self.BAND_DAEMON_MIB[0], self.BAND_DAEMON_MIB[1] + 1, 64)
        ]

        assert unclamped, "the control range is empty — the sweep would pass vacuously"
        assert all(cap <= int(RAM_RESUME_FLOOR_GB * 1024) for cap in unclamped)

    def test_no_emitted_cap_sits_under_the_workload_floor_the_doctor_fails(self) -> None:
        """The other half of the trap: a sub-floor cap is what ``t3 doctor check`` calls broken."""
        floor_mib = DEFAULT_AGENT_WORKLOAD_FLOOR_GIB * 1024
        offenders = {
            daemon: sizing.mem_limit_mib
            for daemon in range(2048, 32769, 64)
            if 0 < (sizing := self._sizing(daemon)).mem_limit_mib < floor_mib
        }

        assert not offenders, f"caps under the doctor's hard floor for daemon totals: {sorted(offenders)[:8]}"

    def test_a_total_that_cannot_hold_a_floor_clearing_cap_refuses(self) -> None:
        """TOO-LOW control: no cap exists here, so the sizer must say so rather than guess.

        Replaces an assertion that a 8192 MiB daemon sizes to 3891 — a cap BELOW the
        doctor's own hard floor, i.e. the head was already emitting a broken cap outside
        the band it was fixing.
        """
        for daemon in (8192, self.MIN_WORKABLE_DAEMON_MIB - 1):
            sizing = self._sizing(daemon)

            assert sizing.mem_limit_mib == 0, daemon
            assert sizing.refusal
            assert str(self.MIN_WORKABLE_DAEMON_MIB) in sizing.refusal

    def test_the_fit_boundary_disagrees_across_one_mib(self) -> None:
        """Neither arm can pass vacuously: the two adjacent totals must answer differently."""
        refused = self._sizing(self.MIN_WORKABLE_DAEMON_MIB - 1)
        sized = self._sizing(self.MIN_WORKABLE_DAEMON_MIB)

        assert refused.refusal
        assert refused.mem_limit_mib == 0
        assert sized.refusal is None
        assert sized.mem_limit_mib > int(RAM_RESUME_FLOOR_GB * 1024)

    def test_a_daemon_total_above_the_band_is_unchanged(self) -> None:
        """TOO-HIGH control: a real box sizes byte-identically — this is no blanket clamp."""
        assert DockerWorkerSizing.worker_mem_limit_mib(total_ram_mib=32000, daemon_ram_mib=0) == 22937

    def test_an_unreadable_basis_still_defers_to_the_compose_default(self) -> None:
        sizing = DockerWorkerSizing.worker_sizing(total_ram_mib=0, daemon_ram_mib=0)

        assert sizing.mem_limit_mib == 0
        assert sizing.refusal is None, "an unreadable basis is no opinion, never a refusal"


class TestTheComposeSizingEmitter:
    """What ``deploy/deploy.sh`` ``eval``s, and how a refusal reaches it."""

    def _emit(self, monkeypatch: pytest.MonkeyPatch, sizing: ram_probe.WorkerSizing) -> tuple[str, str]:
        monkeypatch.setattr(DockerWorkerSizing, "worker_sizing", classmethod(lambda cls, *a, **k: sizing))
        monkeypatch.setattr(DockerWorkerSizing, "worker_cpus", staticmethod(lambda *a, **k: 7))
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(ram_probe.sys, "stdout", out)
        monkeypatch.setattr(ram_probe.sys, "stderr", err)
        ram_probe._emit_compose_sizing()
        return out.getvalue(), err.getvalue()

    def test_a_workable_cap_is_emitted_beside_the_cpus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_WORKER_MEM_LIMIT", raising=False)

        out, err = self._emit(monkeypatch, ram_probe.WorkerSizing(6145))

        assert "TEATREE_WORKER_CPUS=7" in out
        assert "TEATREE_WORKER_MEM_LIMIT=6145m" in out
        assert not err

    def test_a_refusal_exits_three_with_the_reason_on_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit 3, not 1: deploy.sh must tell a refusal from a probe that could not run."""
        monkeypatch.delenv("TEATREE_WORKER_MEM_LIMIT", raising=False)

        with pytest.raises(SystemExit) as exc:
            self._emit(monkeypatch, ram_probe.WorkerSizing(0, refusal="raise the allocation"))

        assert exc.value.code == 3

    def test_an_operator_set_cap_suppresses_both_the_derivation_and_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The never-lockout escape: an explicit value is a decision already made."""
        monkeypatch.setenv("TEATREE_WORKER_MEM_LIMIT", "8g")

        out, err = self._emit(monkeypatch, ram_probe.WorkerSizing(0, refusal="raise the allocation"))

        assert "TEATREE_WORKER_CPUS=7" in out
        assert "TEATREE_WORKER_MEM_LIMIT" not in out, "a derived line would overwrite the operator's own value"
        assert not err

    def test_an_unreadable_basis_emits_cpus_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_WORKER_MEM_LIMIT", raising=False)

        out, err = self._emit(monkeypatch, ram_probe.WorkerSizing(0))

        assert out == "TEATREE_WORKER_CPUS=7\n"
        assert not err
