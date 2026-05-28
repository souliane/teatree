"""Audit mini-loop — outbound audit + failed-E2E posts.

The legacy global outbound audit scanner stays in the always-on
``dispatch`` mini-loop because it has no graceful-degradation path; the
per-overlay failed-E2E and overlay-specific audit verifiers live here.
"""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    from teatree.loop.tick_jobs import _failed_e2e_scanner_for, _ScannerJob  # noqa: PLC0415

    if not backends:
        return []
    jobs: list[Any] = []
    for backend in backends:
        failed_e2e = _failed_e2e_scanner_for(backend)
        if failed_e2e is not None:
            jobs.append(_ScannerJob(scanner=failed_e2e, overlay=backend.name))
    return jobs


MINI_LOOP = MiniLoop(
    name="audit",
    default_cadence_seconds=600,
    build_jobs=_build_jobs,
)
