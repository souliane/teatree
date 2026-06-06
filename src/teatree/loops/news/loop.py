"""News mini-loop — daily ``scanning_news`` cadence anchor."""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _scanning_news_scanner  # noqa: PLC0415
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415

    scanner = _scanning_news_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="news",
    default_cadence_seconds=3600,  # 1h tick rate — daily cadence enforced internally
    build_jobs=_build_jobs,
)
