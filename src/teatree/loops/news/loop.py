"""News mini-loop — daily ``scanning_news`` cadence anchor."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(**_: Any) -> list[Any]:  # noqa: ANN401 — orchestrator passes extra context as open kwargs
    from teatree.loop.tick_jobs import _ScannerJob, _scanning_news_scanner  # noqa: PLC0415

    scanner = _scanning_news_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="news",
    default_cadence_seconds=3600,  # 1h tick rate — daily cadence enforced internally
    build_jobs=_build_jobs,
)
