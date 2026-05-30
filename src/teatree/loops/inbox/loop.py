"""Inbox mini-loop definition.

High-cadence (1m default) scanners that read inbound surfaces: Slack
mentions/DMs, RED CARD signals, Notion view items. Default cadence is
short because user-facing inbox lag is felt within seconds.
"""

from typing import Any

from teatree.loops.base import MiniLoop


def _build_jobs(
    *,
    backends: list[Any] | None = None,
    notion_client: Any | None = None,  # noqa: ANN401 — NotionLike, kept loose
    messaging: Any | None = None,  # noqa: ANN401 — MessagingBackend, kept loose
    **_: Any,  # noqa: ANN401 — orchestrator passes extra context as open kwargs
) -> list[Any]:
    """Delegate to the existing :mod:`teatree.loop.tick_jobs` per-backend wiring.

    Per-overlay slack/red-card scanners live in
    :func:`teatree.loop.tick_jobs._messaging_jobs_for_backend`; the
    notion view scanner lives in :func:`teatree.loop.tick_jobs.build_default_jobs`'s
    notion branch. We reuse both rather than duplicate the wiring.

    ``review_nag`` is excluded here (``include_review_nag=False``) — the
    followup mini-loop is its single owner, so the registry fan-out emits
    one nag per tick, matching the legacy fan-out.
    """
    from teatree.loop.scanners import NotionViewScanner  # noqa: PLC0415
    from teatree.loop.tick_jobs import _messaging_jobs_for_backend, _ScannerJob  # noqa: PLC0415

    jobs: list[Any] = []
    if backends:
        for backend in backends:
            if backend.messaging is not None:
                jobs.extend(_messaging_jobs_for_backend(backend, backend.name, include_review_nag=False))
    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    # Single-overlay messaging path used by tests and ad-hoc CLI.
    if not backends and messaging is not None:
        jobs.extend(_single_overlay_messaging_jobs(messaging))
    return jobs


def _single_overlay_messaging_jobs(messaging: Any) -> list[Any]:  # noqa: ANN401
    from teatree.loop.scanners import (  # noqa: PLC0415
        RedCardScanner,
        SlackDmInboundScanner,
        SlackMentionsScanner,
        SlackReviewIntentScanner,
    )
    from teatree.loop.tick_jobs import _ScannerJob  # noqa: PLC0415

    return [
        _ScannerJob(scanner=SlackMentionsScanner(backend=messaging), overlay=""),
        _ScannerJob(scanner=SlackDmInboundScanner(backend=messaging, overlay=""), overlay=""),
        _ScannerJob(scanner=SlackReviewIntentScanner(backend=messaging, overlay=""), overlay=""),
        _ScannerJob(scanner=RedCardScanner(backend=messaging, overlay=""), overlay=""),
    ]


MINI_LOOP = MiniLoop(
    name="inbox",
    default_cadence_seconds=60,  # 1 minute — inbox lag is user-visible
    build_jobs=_build_jobs,
)
