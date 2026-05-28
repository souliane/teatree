"""Tickets mini-loop — local Ticket DB scanners + per-host disposition/completion."""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    if not backends:
        return []
    jobs: list[Any] = []
    for backend in backends:
        jobs.extend(_per_overlay_jobs(backend))
    return jobs


def _per_overlay_jobs(backend: Any) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import ActiveTicketsScanner, StaleTicketsScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _identity_alias_groups_for_overlay, _ScannerJob  # noqa: PLC0415

    tag = backend.name
    jobs: list[Any] = []
    if backend.external_db is not None:
        from teatree.loop.scanners.external_tickets import ExternalTicketsScanner  # noqa: PLC0415

        jobs.append(
            _ScannerJob(
                scanner=ExternalTicketsScanner(overlay_name=tag, db_path=backend.external_db),
                overlay=tag,
            ),
        )
    else:
        jobs.append(_ScannerJob(scanner=ActiveTicketsScanner(overlay_name=tag), overlay=tag))
    jobs.append(
        _ScannerJob(
            scanner=StaleTicketsScanner(overlay_name=tag, threshold_days=backend.stale_threshold_days),
            overlay=tag,
        ),
    )
    identity_groups = _identity_alias_groups_for_overlay(tag, backend)
    if not identity_groups and len(backend.identities) > 1:
        identity_groups = (tuple(backend.identities),)
    jobs.extend(_per_host_disposition_jobs(backend, tag, identity_groups=identity_groups))
    return jobs


def _per_host_disposition_jobs(
    backend: Any,  # noqa: ANN401
    tag: str,
    *,
    identity_groups: tuple[tuple[str, ...], ...],
) -> list[Any]:
    from teatree.loop.scanners import TicketCompletionScanner, TicketDispositionScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _ScannerJob, _user_identity_aliases_for_overlay  # noqa: PLC0415

    jobs: list[Any] = []
    ticket_completion_emitted = False
    for code_host in backend.hosts:
        jobs.append(
            _ScannerJob(
                scanner=TicketDispositionScanner(
                    host=code_host,
                    overlay=backend.overlay,
                    ready_labels=backend.ready_labels,
                    overlay_name=tag,
                    user_identity_aliases=_user_identity_aliases_for_overlay(tag),
                    identity_alias_groups=identity_groups,
                ),
                overlay=tag,
            ),
        )
        if backend.overlay is not None and not ticket_completion_emitted:
            jobs.append(
                _ScannerJob(
                    scanner=TicketCompletionScanner(overlay=backend.overlay, overlay_name=tag),
                    overlay=tag,
                ),
            )
            ticket_completion_emitted = True
    return jobs


MINI_LOOP = MiniLoop(
    name="tickets",
    default_cadence_seconds=300,
    build_jobs=_build_jobs,
)
