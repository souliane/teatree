"""Scan Slack mentions and DMs delivered by the active overlay's MessagingBackend."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from teatree.backends.protocols import MessagingBackend
from teatree.config import DATA_DIR
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal


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
            signals.append(
                ScanSignal(
                    kind="slack.dm",
                    summary=f"DM {ts}: {_text(event)[:80]}",
                    payload={"ts": ts, "event": event},
                )
            )
            if ts:
                cursors["dms"] = max(cursors.get("dms", ""), ts)
        if signals:
            _write_cursors(self.cursor_path, cursors)
        return signals
