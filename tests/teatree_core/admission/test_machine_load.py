"""The governor's machine reading comes straight from /proc and the cgroup, in every container."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.admission import machine_load
from teatree.core.admission.memory_hold import memory_hold_reason
from teatree.core.admission_governor import MachineBrake, QuotaSignal, decide_admission, read_machine_signal
from teatree.utils import ram_probe, ram_scope
from teatree.utils.ram_scope import read_ram_headroom as read_live_ram_headroom

_KIB_PER_MIB = 1024
_HOST_AVAILABLE_MIB = 12 * 1024
_ADMIN_CGROUP_MIB = 2 * 1024
_WORKER_CGROUP_MIB = 21299


def _quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=False,
        all_accounts_exhausted=False,
        weekly_utilization=0.0,
        short_utilization=0.0,
        seconds_to_weekly_reset=None,
    )


class _StagedBox(TestCase):
    """One /proc/meminfo and one cgroup, read by the real RAM readers the autouse pin replaces."""

    def stage_box(self, *, meminfo_mib: int | None, cgroup_limit_mib: int, cgroup_used_mib: int = 0) -> None:
        meminfo = Path(self.enterContext(tempfile.TemporaryDirectory())) / "meminfo"
        if meminfo_mib is not None:
            meminfo.write_text(
                f"MemTotal: 33554432 kB\nMemAvailable: {meminfo_mib * _KIB_PER_MIB} kB\n", encoding="utf-8"
            )
        cgroup_files = {"memory.max": cgroup_limit_mib, "memory.current": cgroup_used_mib}

        def cgroup_v2_memory_mib(filename: str, *, trace: dict[str, int | str] | None = None) -> int | None:
            return cgroup_files.get(filename)

        self.enterContext(patch.object(ram_probe, "_MEMINFO_PATH", str(meminfo)))
        self.enterContext(patch.object(ram_scope, "read_ram_headroom", read_live_ram_headroom))
        self.enterContext(patch.object(ram_scope, "cgroup_v2_memory_mib", cgroup_v2_memory_mib))
        self.enterContext(patch.object(ram_scope, "cgroup_v1_memory_mib", return_value=None))
        self.enterContext(patch.object(ram_scope, "cgroup_v2_reclaimable_mib", return_value=0))


class TestMemoryIsTheHostFigureInEveryContainer(_StagedBox):
    def test_the_admin_container_is_not_gated_on_its_own_budget(self) -> None:
        self.stage_box(meminfo_mib=_HOST_AVAILABLE_MIB, cgroup_limit_mib=_ADMIN_CGROUP_MIB)

        machine = read_machine_signal()

        assert machine.ram_available_gb == pytest.approx(12.0)
        assert machine.memory_cap_gb is None

    def test_the_worker_reports_the_same_host_figure_and_names_its_own_cap(self) -> None:
        self.stage_box(meminfo_mib=_HOST_AVAILABLE_MIB, cgroup_limit_mib=_WORKER_CGROUP_MIB)

        machine = read_machine_signal()

        assert machine.ram_available_gb == pytest.approx(12.0)
        assert machine.memory_cap_gb == pytest.approx(20.8, abs=0.01)

    def test_a_worker_cgroup_with_less_headroom_than_the_host_caps_the_reading(self) -> None:
        self.stage_box(meminfo_mib=_HOST_AVAILABLE_MIB, cgroup_limit_mib=8 * 1024, cgroup_used_mib=6 * 1024)

        machine = read_machine_signal()
        decision = decide_admission(quota=_quota(), machine=machine)

        assert machine.ram_available_gb == pytest.approx(2.0)
        assert machine.memory_cap_gb == pytest.approx(8.0)
        assert not decision.admit
        assert decision.reason.endswith("(container reading)")

    def test_unreadable_meminfo_is_unknown_never_the_cgroup_number(self) -> None:
        self.stage_box(meminfo_mib=None, cgroup_limit_mib=_WORKER_CGROUP_MIB, cgroup_used_mib=1024)

        machine = read_machine_signal()

        assert machine.ram_available_gb is None
        assert memory_hold_reason(machine) == "memory_pressure (available memory unknown; container reading)"

    def test_an_explicit_reading_wins_and_carries_no_cgroup_cap(self) -> None:
        self.stage_box(meminfo_mib=_HOST_AVAILABLE_MIB, cgroup_limit_mib=_WORKER_CGROUP_MIB)

        machine = read_machine_signal(ram_available_gb=3.5)

        assert machine.ram_available_gb == pytest.approx(3.5)
        assert machine.memory_cap_gb is None
        assert machine.memory_source == ""


class TestLoadIsJudgedOnTheHostCores(TestCase):
    """Three usable cores on a ten-core host: the load average is the host's, so it is judged there."""

    def stage_load(self, load1: float) -> None:
        self.enterContext(patch.object(machine_load.os, "cpu_count", return_value=10))
        self.enterContext(patch.object(machine_load, "read_load_and_cores", return_value=(load1, 3)))

    def test_sizing_uses_the_usable_cores_and_load_the_host_cores(self) -> None:
        self.stage_load(12.0)

        machine = read_machine_signal()

        assert machine.cores == 3
        assert machine.load_scope_cores == 10

    def test_host_load_under_the_host_watermark_admits_despite_a_small_quota(self) -> None:
        self.stage_load(12.0)

        decision = decide_admission(
            quota=_quota(), machine=read_machine_signal(ram_available_gb=12.0), load_brake=MachineBrake(braked=True)
        )

        assert decision.admit, decision.reason

    def test_a_load_refusal_names_the_container_reading(self) -> None:
        self.stage_load(30.0)

        decision = decide_admission(quota=_quota(), machine=read_machine_signal(ram_available_gb=12.0))

        assert not decision.admit
        assert decision.cause == "load"
        assert decision.reason.endswith("(container reading)")
