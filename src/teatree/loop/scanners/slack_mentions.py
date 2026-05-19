"""Scan Slack mentions and DMs from the MessagingBackend and the Socket Mode queue."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import MessagingBackend
from teatree.loop.scanners.base import ScanSignal
from teatree.paths import DATA_DIR
from teatree.types import RawAPIDict


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
    """Surface ``app_mention`` and ``message.im`` events queued by Socket Mode."""

    backend: MessagingBackend
    cursor_path: Path = field(default_factory=_default_cursor_path)
    name: str = "slack_mentions"

    def scan(self) -> list[ScanSignal]:
        cursors = _read_cursors(self.cursor_path)
        mentions = self.backend.fetch_mentions(since=cursors.get("mentions", ""))
        dms = self.backend.fetch_dms(since=cursors.get("dms", ""))

        from teatree.backends.slack_receiver import drain_event_queue  # noqa: PLC0415

        for queued in drain_event_queue():
            event = queued.get("event", {})
            event_type = event.get("type", "")
            if event_type == "app_mention":
                mentions.append(event)
            elif event_type == "message":
                channel_type = event.get("channel_type", "")
                if channel_type == "im":
                    dms.append(event)

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
        if signals:
            _write_cursors(self.cursor_path, cursors)
        return signals
