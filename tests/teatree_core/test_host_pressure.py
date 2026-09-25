"""A fresh host feed wins over a Docker VM reading; stale data never masquerades as fresh."""

import json
import logging
import time
from pathlib import Path

import pytest

from teatree.core import admission_governor
from teatree.core.admission_governor import QuotaSignal, decide_admission, read_machine_signal
from teatree.utils import host_pressure, ram_probe, ram_scope
from teatree.utils.ram_scope import RamHeadroom


def _write_feed(
    tmp_path: Path, *, epoch: int, load1: float = 109.45, ram_available_mib: int = 473, swap_used_mib: int = 1660
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
            }
        ),
        encoding="utf-8",
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
    monkeypatch.setattr(ram_probe, "available_cpu_count", lambda: 16)
    _write_feed(tmp_path, epoch=int(time.time()))
    machine = read_machine_signal()
    assert machine.cores == 10
    assert machine.load1 == pytest.approx(109.45)
    assert machine.ram_available_gb == pytest.approx(473 / 1024)
    assert machine.swap_used_fraction == pytest.approx(1660 / 3072)


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
    monkeypatch.setattr(ram_probe, "available_cpu_count", lambda: 16)

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


def test_fresh_host_load_uses_worker_cpu_quota_for_brake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    _write_feed(tmp_path, epoch=int(time.time()), load1=20.0, ram_available_mib=12 * 1024, swap_used_mib=0)
    monkeypatch.setattr(ram_probe, "available_cpu_count", lambda: 3)
    machine = read_machine_signal()
    decision = decide_admission(quota=_quota(), machine=machine)

    assert machine.cores == 3
    assert not decision.admit
    assert "load" in decision.reason


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
