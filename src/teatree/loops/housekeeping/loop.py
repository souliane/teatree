"""Housekeeping mini-loop — editable self-update + work-repo main clone."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    from teatree.loop.tick_jobs import _pull_main_clone_scanner_for, _ScannerJob, _self_update_scanner  # noqa: PLC0415

    jobs: list[Any] = []
    self_update = _self_update_scanner()
    if self_update is not None:
        jobs.append(_ScannerJob(scanner=self_update, overlay=""))
    if backends:
        for backend in backends:
            pull = _pull_main_clone_scanner_for(backend)
            if pull is not None:
                jobs.append(_ScannerJob(scanner=pull, overlay=backend.name))
    return jobs


MINI_LOOP = MiniLoop(
    name="housekeeping",
    default_cadence_seconds=3600,  # 1h — git pulls are not user-visible
    build_jobs=_build_jobs,
)
