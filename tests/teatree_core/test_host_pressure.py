"""A fresh host feed wins over a Docker VM reading; stale data never masquerades as fresh."""

import json
import logging
import time
from pathlib import Path

import pytest

from teatree.core import admission_governor
from teatree.core.admission_governor import MachineBrake, QuotaSignal, decide_admission, read_machine_signal
from teatree.utils import host_pressure, ram_probe, ram_scope
from teatree.utils.ram_scope import RamHeadroom


@pytest.fixture(autouse=True)
def _pin_absent_cpu_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ram_probe, "cgroup_cpu_quota", lambda: None)


def _write_feed(
    tmp_path: Path,
    *,
    epoch: int,
    load1: float = 109.45,
    ram_available_mib: int = 473,
    swap_used_mib: int = 1660,
    **activity: float,
) -> None:
    (tmp_path / "host-pressure.json").write_text(
        json.dumps(
            {
                "epoch": epoch,
                "cores": 10,
                "load1": load1,
                "ram_available_mib": ram_available_mib,
                "swap_used_mib": swap_used_mib,
                "swap_total_mib": 3072,
                **activity,
            }
        ),
        encoding="utf-8",
    )


def _stub_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ram_scope,
        "read_ram_headroom",
        lambda: RamHeadroom(available_mib=None, cgroup_limit_mib=None, host_available_mib=None),
    )


def _quota() -> QuotaSignal:
    return QuotaSignal(
        fresh=False,
        all_accounts_exhausted=False,
        weekly_utilization=0.0,
        short_utilization=0.0,
        seconds_to_weekly_reset=None,
    )


def test_fresh_host_reading_beats_docker_vm(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()), swap_mib_per_s=12.5, vm_pressure_level=2)
    machine = read_machine_signal()
    assert machine.cores == 10
    assert machine.load1 == pytest.approx(109.45)
    assert machine.ram_available_gb == pytest.approx(473 / 1024)
    assert machine.swap_mib_per_s == pytest.approx(12.5)
    assert machine.vm_pressure_level == 2


def test_eight_core_host_without_cpu_quota_keeps_four_agent_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()))
    feed = json.loads((tmp_path / "host-pressure.json").read_text(encoding="utf-8"))
    feed["cores"] = 8
    (tmp_path / "host-pressure.json").write_text(json.dumps(feed), encoding="utf-8")
    monkeypatch.setattr(ram_probe, "cgroup_cpu_quota", lambda: None)
    _stub_uncapped(monkeypatch)
    machine = read_machine_signal()
    assert machine.cores == 8
    assert admission_governor._machine_ceiling(machine) == 4


def test_cgroup_cpu_quota_caps_host_ceiling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()))
    monkeypatch.setattr(ram_probe, "cgroup_cpu_quota", lambda: 3)
    _stub_uncapped(monkeypatch)
    machine = read_machine_signal()
    assert machine.cores == 3
    assert admission_governor._machine_ceiling(machine) == 1


def test_a_full_swap_file_with_no_swap_activity_admits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The 2026-09-26 reading: macOS keeps 82% of swap allocated long after the pressure
    # that filled it, while memory_pressure reports 61% free and nothing pages.
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _stub_uncapped(monkeypatch)
    (tmp_path / "host-pressure.json").write_text(
        json.dumps(
            {
                "epoch": int(time.time()),
                "cores": 10,
                "load1": 6.07,
                "ram_available_mib": 7581,
                "swap_used_mib": 5056,
                "swap_total_mib": 6144,
                "swap_mib_per_s": 0.02,
                "vm_pressure_level": 1,
            }
        ),
        encoding="utf-8",
    )
    decision = decide_admission(quota=_quota(), machine=read_machine_signal(), load_brake=MachineBrake(braked=True))

    assert decision.admit, decision.reason


def test_a_feed_without_activity_fields_contributes_no_swap_brake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _stub_uncapped(monkeypatch)
    _write_feed(tmp_path, epoch=int(time.time()), load1=1.0, ram_available_mib=12 * 1024, swap_used_mib=3000)
    machine = read_machine_signal()

    assert machine.swap_mib_per_s is None
    assert machine.vm_pressure_level is None
    assert decide_admission(quota=_quota(), machine=machine).admit


def test_stale_host_reading_warns_and_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path, caplog) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()) - 61)
    with caplog.at_level(logging.WARNING):
        machine = read_machine_signal()
    assert machine.load1 != pytest.approx(109.45)
    assert "host pressure feed" in caplog.text


def test_worker_feed_path_override_reads_fresh_host_signal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host_dir = tmp_path / "host-only"
    host_dir.mkdir()
    _write_feed(host_dir, epoch=int(time.time()))
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "factory-data"))
    monkeypatch.setenv("T3_HOST_PRESSURE_PATH", str(host_dir / "host-pressure.json"))

    assert host_pressure.feed_path() == host_dir / "host-pressure.json"
    assert read_machine_signal().load1 == pytest.approx(109.45)


@pytest.mark.parametrize("feed_state", ["absent", "stale"])
def test_worker_feed_path_override_falls_back_when_absent_or_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    feed_state: str,
) -> None:
    host_dir = tmp_path / "host-only"
    host_dir.mkdir()
    if feed_state == "stale":
        _write_feed(host_dir, epoch=int(time.time()) - 61)
    monkeypatch.setenv("T3_HOST_PRESSURE_PATH", str(host_dir / "host-pressure.json"))
    monkeypatch.setattr(admission_governor.machine_load, "read_load_and_cores", lambda: (0.25, 2))

    assert read_machine_signal().load1 == pytest.approx(0.25)


def test_missing_feed_warning_memo_can_be_reset(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing = tmp_path / "host-pressure.json"
    with caplog.at_level(logging.WARNING):
        assert host_pressure.read_host_pressure(path=missing) is None
        assert host_pressure.read_host_pressure(path=missing) is None
        assert caplog.text.count("host pressure feed missing") == 1
        host_pressure.reset_missing_warning_memo()
        assert host_pressure.read_host_pressure(path=missing) is None
    assert caplog.text.count("host pressure feed missing") == 2


def test_host_load_is_judged_against_host_cores_not_the_container_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Load 28 on the 10-core host is under the braked 3/core resume watermark,
    # even when a 3-core cgroup quota caps agent concurrency.
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()), load1=28.0, ram_available_mib=12 * 1024, swap_used_mib=0)
    monkeypatch.setattr(ram_probe, "cgroup_cpu_quota", lambda: 3)
    _stub_uncapped(monkeypatch)
    machine = read_machine_signal()
    decision = decide_admission(quota=_quota(), machine=machine, load_brake=MachineBrake(braked=True))

    assert machine.cores == 3
    assert machine.load_cores == 10
    assert decision.admit, decision.reason


def test_fresh_host_memory_keeps_worker_cgroup_floor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()), load1=1.0, ram_available_mib=12 * 1024, swap_used_mib=0)
    monkeypatch.setattr(
        ram_scope,
        "read_ram_headroom",
        lambda: RamHeadroom(available_mib=2 * 1024, cgroup_limit_mib=8 * 1024, host_available_mib=12 * 1024),
    )
    machine = read_machine_signal()
    decision = decide_admission(quota=_quota(), machine=machine)

    assert machine.ram_available_gb == pytest.approx(2.0)
    assert machine.memory_cap_gb == pytest.approx(8.0)
    assert not decision.admit
    assert "GB" in decision.reason
