"""Dispatch mini-loop definition — always-on global scanners."""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    """Build the always-on global scanner dispatch set.

    Delegates to the ``Domain.DISPATCH`` slice of the public
    :func:`teatree.loop.domain_jobs.jobs_for_domain` seam so the legacy
    fan-out stays the single source of which scanners run in this
    mini-loop. The dispatch set carries no per-overlay state.
    """
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
    from teatree.loop.job_identity import Domain  # noqa: PLC0415

    return jobs_for_domain(Domain.DISPATCH)


MINI_LOOP = MiniLoop(
    name="dispatch",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
    always_on=True,
)
