"""Housekeeping mini-loop — editable self-update + work-repo main clone."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    """Wire the global self-update job + each overlay's pull-main-clone slice.

    Self-update is a global (``overlay=""``) job — it fast-forwards the
    editable installs themselves, not any one overlay's tracked work — so
    it is built directly here, not via the per-overlay seam. The
    per-overlay pull-main-clone scanner is owned by ``Domain.HOUSEKEEPING``.
    """
    from teatree.loop.tick_jobs import Domain, _ScannerJob, _self_update_scanner, jobs_for_domain  # noqa: PLC0415

    jobs: list[Any] = []
    self_update = _self_update_scanner()
    if self_update is not None:
        jobs.append(_ScannerJob(scanner=self_update, overlay=""))
    if backends:
        all_backends = tuple(backends)
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.HOUSEKEEPING, backend, all_backends=all_backends))
    return jobs


MINI_LOOP = MiniLoop(
    name="housekeeping",
    default_cadence_seconds=3600,  # 1h — git pulls are not user-visible
    build_jobs=_build_jobs,
)
