"""The Socket Mode listener's heartbeat file: its path, parse and staleness bound.

``t3 slack listen`` stamps this file from inside each connected overlay loop. It is read by
``t3 doctor``, the inbox catch-up job and :mod:`teatree.core.slack_first.factory_slack_liveness`, so the
read lives here, below every one of them; the writer stays beside the connection that owns it
in :mod:`teatree.backends.slack.listener_heartbeat`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from teatree.paths import DATA_DIR

FILENAME = "slack-listener-heartbeat.json"
HEARTBEAT_INTERVAL_SECONDS = 15
STALE_MULTIPLIER = 4
STALE_FLOOR_SECONDS = 120


def heartbeat_path() -> Path:
    return DATA_DIR / FILENAME


@dataclass(frozen=True, slots=True)
class ListenerBeat:
    """One parsed heartbeat; ``stamps is None`` is a payload written before stamps were per overlay."""

    updated_at: float
    interval_seconds: int
    stamps: Mapping[str, float] | None = None
    legacy_updated_at: float | None = None

    @property
    def stale_after(self) -> int:
        return max(STALE_MULTIPLIER * self.interval_seconds, STALE_FLOOR_SECONDS)

    def stamp_for(self, overlay: str) -> float | None:
        if self.stamps is None:
            return self.updated_at
        return self.stamps.get(overlay, self.legacy_updated_at)


def read_heartbeat(path: Path | None = None) -> ListenerBeat | None:
    """The listener's last heartbeat, or ``None`` when absent or unparsable."""
    path = path or heartbeat_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        overlays = raw.get("overlays")
        stamps = {str(name): float(at) for name, at in overlays.items()} if isinstance(overlays, dict) else None
        return ListenerBeat(
            updated_at=float(raw["updated_at"]),
            interval_seconds=int(raw.get("interval_seconds", 0)),
            stamps=stamps,
            legacy_updated_at=float(raw["legacy_updated_at"]) if "legacy_updated_at" in raw else None,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def heartbeat_is_stale(beat: ListenerBeat | None, overlay: str, *, now: float) -> bool:
    """No file, no stamp for *overlay*, or a stamp older than the beat's staleness bound."""
    stamp = beat.stamp_for(overlay) if beat is not None else None
    if beat is None or stamp is None:
        return True
    return now - stamp > beat.stale_after
