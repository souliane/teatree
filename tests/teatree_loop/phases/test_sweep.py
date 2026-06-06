"""Tests for ``teatree.loop.phases.sweep`` — the maintenance-scanner split."""

from dataclasses import dataclass

import pytest

from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.sweep import SWEEP_SCANNER_NAMES, sweep_phase
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class _NamedScanner:
    name: str

    def scan(self) -> list[ScanSignal]:
        return []


def _job(name: str, overlay: str = "") -> _ScannerJob:
    return _ScannerJob(scanner=_NamedScanner(name=name), overlay=overlay)


def test_sweep_phase_partitions_maintenance_scanners_out_of_scan() -> None:
    jobs = [_job("my_prs"), _job("pr_sweep"), _job("self_update"), _job("pull_main_clone"), _job("pending_tasks")]
    split = sweep_phase(jobs)
    assert {j.scanner.name for j in split.sweep_jobs} == {"pr_sweep", "self_update", "pull_main_clone"}
    assert {j.scanner.name for j in split.scan_jobs} == {"my_prs", "pending_tasks"}


@pytest.mark.parametrize("sweep_name", sorted(SWEEP_SCANNER_NAMES))
def test_sweep_phase_routes_each_known_maintenance_scanner_to_sweep(sweep_name: str) -> None:
    split = sweep_phase([_job(sweep_name)])
    assert [j.scanner.name for j in split.sweep_jobs] == [sweep_name]
    assert split.scan_jobs == []


def test_sweep_phase_preserves_every_job_across_the_split() -> None:
    jobs = [_job("my_prs"), _job("pr_sweep"), _job("reviewer_prs")]
    split = sweep_phase(jobs)
    assert len(split.scan_jobs) + len(split.sweep_jobs) == len(jobs)


def test_sweep_phase_with_no_maintenance_scanners_leaves_scan_intact() -> None:
    jobs = [_job("my_prs"), _job("reviewer_prs")]
    split = sweep_phase(jobs)
    assert len(split.scan_jobs) == 2
    assert split.sweep_jobs == []
