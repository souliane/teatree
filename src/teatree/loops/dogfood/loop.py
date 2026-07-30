"""Dogfood mini-loop — overlay-provision-smoke cadence."""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _dogfood_smoke_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at tick time, not import

    scanner = _dogfood_smoke_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="dogfood",
    default_cadence_seconds=3600,  # 1h tick rate — daily cadence enforced internally
    build_jobs=_build_jobs,
)
