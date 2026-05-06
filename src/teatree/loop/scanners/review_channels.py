"""Scan messaging-backend channels for new review-request messages.

Surfaces ``review_channel.request`` signals that the dispatcher folds into
the reviewer-PR queue (no separate agent invocation, BLUEPRINT § 5.6).
"""

import re
from dataclasses import dataclass

from teatree.backends.protocols import MessagingBackend
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal

_PR_URL_RE = re.compile(r"https?://[^\s>|]+/(?:merge_requests|pull|pulls)/\d+")


def _extract_pr_url(text: str) -> str:
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else ""


def _text(event: RawAPIDict) -> str:
    value = event.get("text")
    return value if isinstance(value, str) else ""


def _ts(event: RawAPIDict) -> str:
    value = event.get("ts") or event.get("event_ts")
    return value if isinstance(value, str) else ""


@dataclass(slots=True)
class ReviewChannelsScanner:
    """Inspect mentions/DMs for review-request URLs and emit folded signals."""

    backend: MessagingBackend
    name: str = "review_channels"

    def scan(self) -> list[ScanSignal]:
        events = self.backend.fetch_mentions() + self.backend.fetch_dms()
        signals: list[ScanSignal] = []
        for event in events:
            text = _text(event)
            url = _extract_pr_url(text)
            if not url:
                continue
            signals.append(
                ScanSignal(
                    kind="review_channel.request",
                    summary=f"Review request: {url}",
                    payload={"url": url, "ts": _ts(event), "event": event},
                )
            )
        return signals
