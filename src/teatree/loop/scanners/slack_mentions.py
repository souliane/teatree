"""Scan Slack mentions and DMs from the MessagingBackend and the Socket Mode queue."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.backend_protocols import MessagingBackend
from teatree.loop.scanners.base import ScanSignal
from teatree.paths import DATA_DIR
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _default_cursor_path() -> Path:
    return DATA_DIR / "loop" / "slack_cursor.json"


def _read_cursors(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _write_cursors(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ts(event: RawAPIDict) -> str:
    ts = event.get("ts") or event.get("event_ts")
    return ts if isinstance(ts, str) else ""


def _text(event: RawAPIDict) -> str:
    text = event.get("text")
    return text if isinstance(text, str) else ""


def _channel(event: RawAPIDict) -> str:
    channel = event.get("channel")
    return channel if isinstance(channel, str) else ""


def _resolve_permalink(backend: MessagingBackend, channel: str, ts: str) -> str:
    """Return ``backend.get_permalink`` result, or empty on any failure.

    The statusline renderer needs a clickable Slack URL but a transient
    Slack outage must not break statusline rendering — fall back to the
    bare ``ts`` label when the lookup fails (see #1050).
    """
    if not channel or not ts:
        return ""
    try:
        return backend.get_permalink(channel=channel, ts=ts)
    except Exception:  # noqa: BLE001 — permalink is decoration, never a hard failure
        return ""


@dataclass(slots=True)
class SlackMentionsScanner:
    """Surface ``app_mention`` and ``message.im`` events queued by Socket Mode.

    Side-effect for mentions referencing an MR/PR URL (#1047): record a
    :class:`ReviewAssignment` row and post the ``:eyes:`` reaction on the
    user's behalf via :func:`record_mention_intent`. Idempotent — the row
    is keyed on ``(overlay, mr_url, user_id)`` so re-firing on the same
    mention is a no-op. The existing ``slack.mention`` signal still routes
    to ``t3:reviewer`` via the dispatcher; the side effect just persists
    the ledger and acks the user.
    """

    backend: MessagingBackend
    cursor_path: Path = field(default_factory=_default_cursor_path)
    overlay: str = ""
    name: str = "slack_mentions"

    def scan(self) -> list[ScanSignal]:
        cursors = _read_cursors(self.cursor_path)
        mentions = self.backend.fetch_mentions(since=cursors.get("mentions", ""))
        dms = self.backend.fetch_dms(since=cursors.get("dms", ""))

        drained_any = self._drain_queue_into(mentions, dms)

        signals: list[ScanSignal] = []
        for event in mentions:
            ts = _ts(event)
            signals.append(
                ScanSignal(
                    kind="slack.mention",
                    summary=f"Mention {ts}: {_text(event)[:80]}",
                    payload={"ts": ts, "event": event},
                )
            )
            self._record_review_intent(event)
            if ts:
                cursors["mentions"] = max(cursors.get("mentions", ""), ts)
        for event in dms:
            ts = _ts(event)
            channel = _channel(event)
            permalink = _resolve_permalink(self.backend, channel, ts)
            signals.append(
                ScanSignal(
                    kind="slack.dm",
                    summary=f"DM {ts}: {_text(event)[:80]}",
                    payload={"ts": ts, "event": event, "permalink": permalink},
                )
            )
            if ts:
                cursors["dms"] = max(cursors.get("dms", ""), ts)
        if signals or drained_any:
            # Persist the cursor advance before discarding the backing file.
            # Decoupled from ``signals``: drained events that match no
            # signal-producing type still advance the cursor so those events
            # are not re-fetched on the next tick.
            _write_cursors(self.cursor_path, cursors)
        if drained_any:
            # Discard the backing file only after the durable cursor persist
            # above. A crash before this point leaves the ``.draining`` file
            # for the next drain to recover, so no mention is lost (Slack
            # never retries them).
            from teatree.backends.slack.receiver import commit_drain  # noqa: PLC0415

            commit_drain()
        return signals

    @staticmethod
    def _drain_queue_into(mentions: list[RawAPIDict], dms: list[RawAPIDict]) -> bool:
        """Fold queued Socket Mode events into the fetched mention/DM lists.

        Returns whether the JSONL queue yielded anything, so :meth:`scan`
        commits the backing file only after persisting. ``reaction_added``
        events land in ``slack-reactions.jsonl`` (drained by
        ``SlackReviewIntentScanner``, #1047) and never reach here.
        """
        from teatree.backends.slack.receiver import drain_event_queue  # noqa: PLC0415

        drained_any = False
        for queued in drain_event_queue():
            drained_any = True
            event = queued.get("event", {})
            event_type = event.get("type", "")
            if event_type == "app_mention":
                mentions.append(event)
            elif event_type == "message" and event.get("channel_type", "") == "im":
                dms.append(event)
        return drained_any

    def _record_review_intent(self, event: RawAPIDict) -> None:
        """Persist a ``ReviewAssignment`` row and post ``:eyes:`` for MR-bearing mentions.

        Best-effort: any failure (DB unavailable in a test, Slack outage)
        logs and continues so the mention scanner never blocks on a
        side-effect. The maker/checker boundary (BLUEPRINT §17.8) is
        preserved because the actual review dispatch happens through the
        dispatcher's ``review_request_dispatch`` on the ``slack.mention``
        signal — this side-effect just persists the ledger and acks.
        """
        try:
            from teatree.loop.scanners.slack_review_intent import record_mention_intent  # noqa: PLC0415

            record_mention_intent(event, backend=self.backend, overlay=self.overlay)
        except Exception:
            logger.exception("Failed to record review intent for mention %s", _ts(event))
