"""Ship mini-loop — own-author PR + GitLab approvals."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    host: Any | None = None,  # noqa: ANN401 — CodeHostBackend, kept loose
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    """Build per-host MyPrsScanner + optional GitLab approvals scanner."""
    from teatree.loop.scanners import MyPrsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _gitlab_approvals_enabled, _ScannerJob  # noqa: PLC0415

    if backends:
        gitlab_enabled = _gitlab_approvals_enabled()
        jobs: list[Any] = []
        for backend in backends:
            jobs.extend(_per_host_jobs(backend, gitlab_enabled=gitlab_enabled))
        return jobs
    if host is not None:
        return [_ScannerJob(scanner=MyPrsScanner(host=host), overlay="")]
    return []


def _per_host_jobs(backend: Any, *, gitlab_enabled: bool) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import GitLabApprovalsScanner, MyPrsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    jobs: list[Any] = []
    for code_host in backend.hosts:
        jobs.append(
            _ScannerJob(
                scanner=MyPrsScanner(host=code_host, identities=backend.identities),
                overlay=backend.name,
            ),
        )
        if gitlab_enabled:
            jobs.append(
                _ScannerJob(
                    scanner=GitLabApprovalsScanner(host=code_host, identities=backend.identities),
                    overlay=backend.name,
                ),
            )
    return jobs


MINI_LOOP = MiniLoop(
    name="ship",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
)
