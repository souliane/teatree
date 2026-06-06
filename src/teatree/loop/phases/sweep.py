"""``sweep_phase`` — the mechanical-maintenance scanner slice.

``pr_sweep`` (auto-merge green solo PRs), ``self_update`` (ff-only update
of the editable installs), and ``pull_main_clone`` (ff-only update of the
work-repo main clones) are maintenance, not world-scan. This phase pulls
them out of the scan fan-out into a named slice so the tick's three jobs
— scan the world, sweep ready maintenance, drive new work — are legible.

The split is behaviour-preserving: the same jobs still run in the same
tick (the caller runs both slices through :func:`scan_phase`) and their
signals merge before dispatch, exactly as when they were three jobs in
the undifferentiated parallel set.
"""

from dataclasses import dataclass, field

from teatree.loop.job_identity import _ScannerJob

SWEEP_SCANNER_NAMES: frozenset[str] = frozenset({"pr_sweep", "self_update", "pull_main_clone"})


@dataclass(slots=True)
class SweepSplit:
    scan_jobs: list[_ScannerJob] = field(default_factory=list)
    sweep_jobs: list[_ScannerJob] = field(default_factory=list)


def sweep_phase(jobs: list[_ScannerJob]) -> SweepSplit:
    split = SweepSplit()
    for job in jobs:
        if job.scanner.name in SWEEP_SCANNER_NAMES:
            split.sweep_jobs.append(job)
        else:
            split.scan_jobs.append(job)
    return split
