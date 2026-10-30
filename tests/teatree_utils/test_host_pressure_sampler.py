import json
import os
from pathlib import Path

import pytest

from teatree.utils.host_pressure import feed_fault, read_host_pressure
from teatree.utils.host_pressure_sampler import HostSampler


def _proc(root: Path, *, available_kib: int = 12_000_000, swapped_pages: int = 0) -> None:
    root.mkdir(exist_ok=True)
    (root / "meminfo").write_text(f"MemAvailable: {available_kib} kB\nSwapTotal: 2048000 kB\nSwapFree: 1024000 kB\n")
    (root / "vmstat").write_text(f"pswpin {swapped_pages}\npswpout 0\n")
    (root / "loadavg").write_text("1.25 0.50 0.25 1/200 300\n")


def test_sampler_publishes_fresh_feed_and_throttles_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEATREE_HOST_OS", "Linux")
    proc = tmp_path / "proc"
    _proc(proc)
    target = tmp_path / "data" / "host-pressure.json"
    sampler = HostSampler(proc_root=proc, path=target)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)

    sampler.publish_if_due(now=1_000)
    first = target.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o644
    assert read_host_pressure(path=target, now=1_001) is not None
    assert json.loads(first)["ram_available_mib"] == 12_000_000 // 1024
    assert json.loads(first)["load1"] == pytest.approx(1.25)
    sampler.publish_if_due(now=1_010)
    assert target.read_bytes() == first
    sampler.publish_if_due(now=1_016)
    assert json.loads(target.read_text())["epoch"] == 1_016


def test_sampler_replaces_feed_with_unrepresentable_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEATREE_HOST_OS", "Linux")
    proc = tmp_path / "proc"
    _proc(proc)
    target = tmp_path / "host-pressure.json"
    target.write_text(json.dumps({"epoch": 1_000, "cores": 8, "load1": int("9" * 400), "ram_available_mib": 1000}))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    HostSampler(proc_root=proc, path=target).publish_if_due(now=1_001)
    assert json.loads(target.read_text())["load1"] == pytest.approx(1.25)


def test_sampler_swap_rate_requires_comparable_samples(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEATREE_HOST_OS", "Linux")
    proc = tmp_path / "proc"
    _proc(proc, swapped_pages=100)
    target = tmp_path / "host-pressure.json"
    sampler = HostSampler(proc_root=proc, path=target)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    sampler.publish_if_due(now=1_000)
    assert "swap_mib_per_s" not in json.loads(target.read_text())

    _proc(proc, swapped_pages=356)
    sampler.publish_if_due(now=1_016)
    assert json.loads(target.read_text())["swap_mib_per_s"] == pytest.approx(
        256 * os.sysconf("SC_PAGE_SIZE") / 1048576 / 16
    )

    _proc(proc, swapped_pages=1)
    sampler.publish_if_due(now=1_032)
    assert "swap_mib_per_s" not in json.loads(target.read_text())

    _proc(proc, swapped_pages=500)
    sampler.publish_if_due(now=1_100)
    assert "swap_mib_per_s" not in json.loads(target.read_text())


@pytest.mark.parametrize(
    ("contents", "now", "fault"),
    [
        (None, 1_000, "missing"),
        ("{", 1_000, "malformed"),
        ('{"epoch":900}', 1_000, "stale"),
        ('{"epoch":1100}', 1_000, "future"),
    ],
)
def test_feed_fault_names_missing_malformed_stale_and_future(
    tmp_path: Path, contents: str | None, now: float, fault: str
) -> None:
    target = tmp_path / "host-pressure.json"
    if contents is not None:
        target.write_text(contents)
    assert fault in feed_fault(path=target, now=now)
