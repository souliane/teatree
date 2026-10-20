"""Socket Mode listener liveness: per-overlay heartbeat stamps and the catch-up a hello owes.

``t3 slack listen`` stamps one file on the shared data mount from inside each connected
overlay loop; ``t3 doctor`` and the inbox catch-up job read it from other containers. A
stamp means "this overlay's socket is live AND everything before this instant is
ingested", so it is withheld while the catch-up a ``hello`` owes has not succeeded.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.core.slack_listener_heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALE_MULTIPLIER,
    heartbeat_path,
    read_heartbeat,
)

if TYPE_CHECKING:
    from teatree.backends.slack.receiver import DeliveredEvents

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "RETRY_BACKOFF_SECONDS",
    "STALE_MULTIPLIER",
    "ConnectionLiveness",
    "OnConnect",
    "write_heartbeat",
]

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0, 120.0)

_write_lock = threading.Lock()

#: Called with ``(overlay_name, alive_at, seen)`` while a hello's catch-up is owed; raising retries it.
type OnConnect = Callable[[str, float, DeliveredEvents], None]


def write_heartbeat(overlay: str, path: Path | None = None) -> float:
    """Stamp *overlay* as live now; the other overlays' stamps in the shared file are kept."""
    path = path or heartbeat_path()
    at = time.time()
    with _write_lock:
        current = read_heartbeat(path)
        stamps = dict(current.stamps or {}) if current is not None else {}
        stamps[overlay] = at
        payload = {
            "updated_at": int(max(stamps.values())),
            "interval_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "overlays": stamps,
        }
        if current is not None:
            legacy_at = current.legacy_updated_at or (current.updated_at if current.stamps is None else None)
            if legacy_at is not None:
                payload["legacy_updated_at"] = legacy_at
        scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            scratch.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            scratch.replace(path)
        except OSError:
            logger.warning("Could not stamp the slack-listener heartbeat at %s", path, exc_info=True)
    return at


@dataclass(slots=True)
class ConnectionLiveness:
    """One overlay connection's catch-up debt and the stamping it gates.

    *seed* is the pre-restart stamp, read before any overlay thread could restamp it.
    The first ``hello`` owes it; a later ``hello`` owes the last stamp of this
    process. Only a successful *on_connect* pass clears the debt, and nothing is
    stamped while it is owed or before the current session has greeted. With no
    *on_connect* the connection stamps whenever its socket is connected.
    """

    seed: float | None = None
    on_connect: OnConnect | None = None
    heartbeat_path: Path | None = None
    owed_from: float | None = field(default=None, init=False)
    greeted_session: str | None = field(default=None, init=False)
    last_stamped_at: float | None = field(default=None, init=False)
    _greeted: bool = field(default=False, init=False)
    _greeted_during_pass: bool = field(default=False, init=False)
    _failures: int = field(default=0, init=False)
    _next_attempt_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def greet(self, session_id: str | None) -> None:
        with self._lock:
            candidate = self.last_stamped_at if self._greeted else self.seed
            self._greeted = True
            self._greeted_during_pass = True
            self.greeted_session = session_id
            if candidate is not None:
                self.owed_from = candidate if self.owed_from is None else min(self.owed_from, candidate)

    def catch_up_due(self, now: float) -> float | None:
        with self._lock:
            if self.owed_from is None or self.greeted_session is None or now < self._next_attempt_at:
                return None
            self._greeted_during_pass = False
            return self.owed_from

    def succeeded(self, started_at: float) -> None:
        with self._lock:
            self.owed_from = started_at if self._greeted_during_pass else None
            self._failures = 0
            self._next_attempt_at = 0.0

    def failed(self, now: float) -> None:
        with self._lock:
            self._next_attempt_at = now + RETRY_BACKOFF_SECONDS[min(self._failures, len(RETRY_BACKOFF_SECONDS) - 1)]
            self._failures += 1

    def may_stamp(self, current_session_id: str | None) -> bool:
        if self.on_connect is None:
            return True
        with self._lock:
            return (
                self.owed_from is None
                and self.greeted_session is not None
                and current_session_id == self.greeted_session
            )

    def stamped(self, at: float) -> None:
        with self._lock:
            self.last_stamped_at = at
