"""Inbox mini-loop definition.

High-cadence (1m default) scanners that read inbound surfaces: Slack
mentions/DMs, RED CARD signals, Notion view items. Default cadence is
short because user-facing inbox lag is felt within seconds.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.loop.job_identity import _ScannerJob
    from teatree.loop.scanners.notion_view import NotionLike


def _build_jobs(
    *,
    backends: "list[OverlayBackends] | None" = None,
    notion_client: "NotionLike | None" = None,
    messaging: "MessagingBackend | None" = None,
    **_: object,
) -> "list[_ScannerJob]":
    """Consume the per-overlay ``Domain.INBOX`` slice plus the global notion job.

    ``Domain.INBOX`` owns the per-overlay inbound Slack scanners
    (mentions / DM / review-intent / red-card) and excludes ``review_nag``
    — the followup mini-loop is its single owner, so the registry fan-out
    emits one nag per tick, matching the legacy fan-out. The notion view
    scanner and the single-overlay messaging path are global / ad-hoc and
    are not part of the per-overlay fan-out, so they stay wired here.
    """
    from teatree.loop.domain_jobs import jobs_for_domain  # noqa: PLC0415
    from teatree.loop.job_identity import Domain, _ScannerJob  # noqa: PLC0415
    from teatree.loop.scanners import NotionViewScanner  # noqa: PLC0415

    jobs: list[_ScannerJob] = []
    if backends:
        all_backends = tuple(backends)
        for backend in backends:
            jobs.extend(jobs_for_domain(Domain.INBOX, backend, all_backends=all_backends))
    if notion_client is not None:
        jobs.append(_ScannerJob(scanner=NotionViewScanner(client=notion_client), overlay=""))
    if not backends and messaging is not None:
        jobs.extend(_single_overlay_messaging_jobs(messaging))
    return jobs


def _single_overlay_messaging_jobs(messaging: "MessagingBackend") -> "list[_ScannerJob]":
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415
    from teatree.loop.scanners import (  # noqa: PLC0415
        RedCardScanner,
        SlackDmInboundScanner,
        SlackMentionsScanner,
        SlackReviewIntentScanner,
    )

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
