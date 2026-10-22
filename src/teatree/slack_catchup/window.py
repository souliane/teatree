"""The catch-up window: where replay starts, and how far back roots and reactions are read."""

import datetime as dt
import logging
from dataclasses import dataclass

from teatree.backends.slack.listener_heartbeat import HEARTBEAT_INTERVAL_SECONDS, STALE_MULTIPLIER

logger = logging.getLogger(__name__)

#: How long the socket may already have been dead when its last stamp was written.
MARGIN_SECONDS = STALE_MULTIPLIER * HEARTBEAT_INTERVAL_SECONDS
#: About twice the measured longest gap between a DM and the reply or owner reaction landing on it.
ROOT_LOOKBACK_SECONDS = 24 * 3600
#: Kept below the consumed-row retention, so a pruned row can never be re-recorded as new.
MAX_LOOKBACK_SECONDS = 7 * 24 * 3600
SLICE_SECONDS = ROOT_LOOKBACK_SECONDS


class CatchUpRefusedError(RuntimeError):
    """The pass cannot read its whole window, so it delivers nothing and the caller retries."""


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).isoformat(timespec="seconds")


def ts_seconds(value: object) -> float:
    """A Slack ``ts`` as epoch seconds; ``0.0`` when it is not one."""
    try:
        return float(value) if isinstance(value, str | int | float) else 0.0
    except ValueError:
        return 0.0


@dataclass(frozen=True, slots=True)
class CatchUpWindow:
    start: float
    oldest: float
    end: float

    @classmethod
    def since(cls, alive_at: float | None, *, now: float) -> "CatchUpWindow":
        floor = now - MAX_LOOKBACK_SECONDS
        if alive_at is None:
            logger.info("Slack catch-up has no liveness evidence; walking back to %s", _iso(floor))
            alive_at = floor
        alive_at = min(alive_at, now)
        if alive_at < floor:
            logger.warning(
                "Slack catch-up cannot recover %s .. %s: older than the %d-day cap",
                _iso(alive_at),
                _iso(floor),
                MAX_LOOKBACK_SECONDS // 86400,
            )
            alive_at = floor
        start = alive_at - MARGIN_SECONDS
        return cls(start=start, oldest=start - ROOT_LOOKBACK_SECONDS, end=now)

    def includes(self, ts: object) -> bool:
        return self.start <= ts_seconds(ts) < self.end

    @property
    def start_ts(self) -> str:
        return f"{self.start:.6f}"

    @property
    def oldest_ts(self) -> str:
        return f"{self.oldest:.6f}"
