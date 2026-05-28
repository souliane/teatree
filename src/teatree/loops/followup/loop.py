"""Followup mini-loop — assigned-issue intake + review-nag cadence."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    host: Any | None = None,  # noqa: ANN401 — CodeHostBackend, kept loose to avoid backend imports
    ready_labels: tuple[str, ...] = (),
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    from teatree.loop.scanners import AssignedIssuesScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    if backends:
        jobs: list[Any] = []
        for backend in backends:
            jobs.extend(_assigned_issues_jobs(backend))
            jobs.extend(_review_nag_jobs(backend))
        return jobs
    if host is not None:
        return [_ScannerJob(scanner=AssignedIssuesScanner(host=host, ready_labels=ready_labels), overlay="")]
    return []


def _assigned_issues_jobs(backend: Any) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import AssignedIssuesScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    return [
        _ScannerJob(
            scanner=AssignedIssuesScanner(
                host=code_host,
                ready_labels=backend.ready_labels,
                exclude_labels=backend.exclude_labels,
                auto_start=backend.auto_start_assigned_issues,
                max_concurrent=backend.max_concurrent_auto_starts,
                overlay_name=backend.name,
                identities=backend.identities,
            ),
            overlay=backend.name,
        )
        for code_host in backend.hosts
    ]


def _review_nag_jobs(backend: Any) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import ReviewNagScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob, _user_slack_id_for_overlay  # noqa: PLC0415

    if backend.messaging is None:
        return []
    return [
        _ScannerJob(
            scanner=ReviewNagScanner(
                messaging=backend.messaging,
                user_slack_id=_user_slack_id_for_overlay(backend.name),
            ),
            overlay=backend.name,
        ),
    ]


MINI_LOOP = MiniLoop(
    name="followup",
    default_cadence_seconds=600,  # 10m — intake is not bursty
    build_jobs=_build_jobs,
)
