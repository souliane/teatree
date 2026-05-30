"""Review mini-loop — reviewer-PR / Slack-review-intent / broadcast / codex / nag wiring.

Five-minute default cadence. Reviewer-PR work is event-bursty (a
colleague pushes a new SHA, the loop fires once, the work is done) so
sub-minute polling buys nothing.
"""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    host: Any | None = None,  # noqa: ANN401 — CodeHostBackend, kept loose
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    """Build per-overlay reviewer-PR + companion review-related scanners."""
    from teatree.loop.scanners import ReviewerPrsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    if backends:
        all_backends = tuple(backends)
        jobs: list[Any] = []
        for backend in backends:
            jobs.extend(_reviewer_per_host_jobs(backend, all_backends=all_backends))
            jobs.extend(_companion_review_jobs(backend))
        return jobs
    if host is not None:
        return [_ScannerJob(scanner=ReviewerPrsScanner(host=host), overlay="")]
    return []


def _reviewer_per_host_jobs(backend: Any, *, all_backends: tuple[Any, ...]) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import ReviewerPrsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _competing_url_prefixes, _ScannerJob  # noqa: PLC0415
    from teatree.loop.tick_resolvers import _allowed_url_prefixes_for_host  # noqa: PLC0415

    jobs: list[Any] = []
    for code_host in backend.hosts:
        url_prefixes = _allowed_url_prefixes_for_host(backend, code_host)
        competing_prefixes = _competing_url_prefixes(
            this_backend=backend,
            code_host=code_host,
            all_backends=all_backends,
        )
        jobs.append(
            _ScannerJob(
                scanner=ReviewerPrsScanner(
                    host=code_host,
                    identities=backend.identities,
                    overlay_name=backend.name,
                    allowed_url_prefixes=url_prefixes,
                    competing_url_prefixes=competing_prefixes,
                ),
                overlay=backend.name,
            ),
        )
    return jobs


def _companion_review_jobs(backend: Any) -> list[Any]:  # noqa: ANN401
    from teatree.loop.tick_jobs import (  # noqa: PLC0415
        _codex_review_scanner_for,
        _pr_sweep_scanner_for,
        _ScannerJob,
        _slack_broadcasts_scanner_for,
        _user_slack_id_for_overlay,
    )

    jobs: list[Any] = []
    broadcasts = _slack_broadcasts_scanner_for(backend)
    if broadcasts is not None:
        jobs.append(_ScannerJob(scanner=broadcasts, overlay=backend.name))
    codex = _codex_review_scanner_for(backend)
    if codex is not None:
        jobs.append(_ScannerJob(scanner=codex, overlay=backend.name))
    sweep = _pr_sweep_scanner_for(backend, slack_user_id=_user_slack_id_for_overlay(backend.name))
    if sweep is not None:
        jobs.append(_ScannerJob(scanner=sweep, overlay=backend.name))
    return jobs


MINI_LOOP = MiniLoop(
    name="review",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
)
