"""Ship mini-loop — own-author PR + GitLab approvals."""

from typing import TYPE_CHECKING

from teatree.core.review.pr_inventory import mark_inventory_unknown, write_inventory
from teatree.loop.scanners import MyPrsScanner
from teatree.loop.scanners.pr_findings import RecordedCriticFindingReader, RecordedVerdictReader
from teatree.loop.tick_resolvers import _web_origin_for_host
from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    host: "CodeHostBackend | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    """Build the unconditional per-host ship scanners."""
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415 — deferred: loaded at tick time, not import
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time

    if backends:
        all_backends = tuple(backends)
        jobs: list[_ScannerJob] = []
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.SHIP, backend, all_backends=all_backends))
        return jobs
    if host is not None:
        return [
            _ScannerJob(
                scanner=MyPrsScanner(
                    host=host,
                    overlay_name="",
                    host_origin=_web_origin_for_host(host),
                    inventory_writer=write_inventory,
                    inventory_invalidator=mark_inventory_unknown,
                    verdict_reader=RecordedVerdictReader(),
                    critic_reader=RecordedCriticFindingReader(),
                ),
                overlay="",
            )
        ]
    return []


MINI_LOOP = MiniLoop(
    name="ship",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.INGRESS, LoopReach.COLLEAGUE}),
    determinism=LoopDeterminism.AI,
)
