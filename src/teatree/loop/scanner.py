"""Scanner ordering and dependency management for the loop tick.

Some scanners must observe the side effects of another scanner within the
same tick. ``slack_review_intent`` reads the in-memory reactions queue that
``slack_mentions`` populates, so the latter must *complete* before the former
begins. The scan phase fans scanners out across a thread pool, so list order
alone does not enforce this — a dependent scanner can start before its
depended-upon scanner finishes. ``serial_scanner_chains`` collapses each
dependency pair into a single unit the pool runs in one worker, preserving the
invariant while leaving independent scanners parallel. Dependencies are keyed
on the scanner ``name`` so the constraint is stable regardless of class
construction or import order.
"""

from dataclasses import dataclass

from teatree.loop.job_identity import _ScannerJob


@dataclass(frozen=True, slots=True)
class ScannerDependency:
    depended_upon: str
    dependent: str


_SCANNER_DEPENDENCIES: tuple[ScannerDependency, ...] = (
    ScannerDependency(depended_upon="slack_mentions", dependent="slack_review_intent"),
)


def _depended_upon_for(scanner_name: str) -> str | None:
    for dep in _SCANNER_DEPENDENCIES:
        if dep.dependent == scanner_name:
            return dep.depended_upon
    return None


def serial_scanner_chains(jobs: list[_ScannerJob]) -> list[list[_ScannerJob]]:
    """Group jobs into units the scan pool runs serially within one worker.

    Each returned list is one execution unit. A standalone scanner yields a
    single-element unit; a dependent scanner is appended to the unit of its
    depended-upon scanner — sharing the same ``overlay`` — so the pool runs
    the pair in order on one worker. A dependent whose depended-upon scanner
    is absent for this tick falls back to its own unit.

    Input order is otherwise preserved, keeping the partition deterministic.
    """
    chains: list[list[_ScannerJob]] = []
    chain_by_anchor: dict[tuple[str, str], list[_ScannerJob]] = {}

    deferred: list[_ScannerJob] = []
    for job in jobs:
        if _depended_upon_for(job.scanner.name) is not None:
            deferred.append(job)
            continue
        unit = [job]
        chains.append(unit)
        chain_by_anchor[job.scanner.name, job.overlay] = unit

    for job in deferred:
        depended_upon_name = _depended_upon_for(job.scanner.name)
        if depended_upon_name is None:
            chains.append([job])
            continue
        anchor = chain_by_anchor.get((depended_upon_name, job.overlay))
        if anchor is None:
            chains.append([job])
        else:
            anchor.append(job)

    return chains
