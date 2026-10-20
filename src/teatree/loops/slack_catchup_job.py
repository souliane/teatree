"""Durable, bounded Slack DM replay shared by the hello hook and inbox job."""

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from teatree import paths
from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.receiver import DeliveredEvents, DeliveryContext, QueuePaths, deliver_event
from teatree.backends.slack.self_identity import resolve_own_identity
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.slack_first.slack_listener_heartbeat import heartbeat_is_stale, read_heartbeat
from teatree.loop.scanners.base import ScanSignal
from teatree.slack_catchup import CatchUpSlice, catch_up

logger = logging.getLogger(__name__)
JOB_BUDGET_SECONDS = 20.0

type RecordDm = Callable[[str, dict], bool]
type Wake = Callable[[], object]


class SlackCatchUpBackendUnavailableError(RuntimeError):
    pass


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class CatchUpState:
    caught_up_at: float | None = None
    seen: DeliveredEvents = field(default_factory=DeliveredEvents)

    @staticmethod
    def path(overlay: str) -> Path:
        return paths.DATA_DIR / "loop" / f"slack-catchup-{quote(overlay, safe='') or 'default'}.json"

    @classmethod
    def memo_path(cls, overlay: str) -> Path:
        return cls.path(overlay).with_suffix(".seen.json")

    @classmethod
    def load_memo(cls, overlay: str) -> dict[str, float]:
        try:
            raw = json.loads(cls.memo_path(overlay).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as error:
            logger.warning("Slack delivery memo unreadable for %s: %s", overlay or "<default>", error)
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, int | float) for key, value in raw.items()
        ):
            logger.warning("Slack delivery memo invalid for %s", overlay or "<default>")
            return {}
        return raw

    @classmethod
    def save_memo(cls, overlay: str, values: dict[str, float]) -> None:
        _write_atomic(cls.memo_path(overlay), values)

    @classmethod
    def from_json(cls, raw: dict) -> "CatchUpState":
        caught_up_at = raw["caught_up_at"]
        seen = raw["seen"]
        valid_cursor = caught_up_at is None or isinstance(caught_up_at, int | float)
        valid_seen = isinstance(seen, dict) and all(
            isinstance(key, str) and isinstance(value, int | float) for key, value in seen.items()
        )
        if not valid_cursor or not valid_seen:
            message = "invalid Slack catch-up state"
            raise ValueError(message)
        return cls(caught_up_at=caught_up_at, seen=DeliveredEvents.from_mapping(seen))

    @classmethod
    def load(cls, overlay: str) -> "CatchUpState":
        path = cls.path(overlay)
        try:
            state = cls.from_json(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            state = cls()
        except (OSError, ValueError, TypeError, KeyError) as error:
            logger.warning("Slack catch-up state unreadable for %s: %s", overlay or "<default>", error)
            state = cls()
        state.seen.update(cls.load_memo(overlay))
        return state

    def save(self, overlay: str) -> None:
        _write_atomic(self.path(overlay), {"caught_up_at": self.caught_up_at, "seen": self.seen.as_mapping()})


def run_catch_up(
    overlay: str, *, alive_at: float | None, seen: DeliveredEvents, record_dm: RecordDm, wake: Wake
) -> Iterator[CatchUpSlice]:
    backend = messaging_from_overlay(overlay or None)
    if not isinstance(backend, SlackBotBackend):
        message = f"Slack backend unavailable for overlay {overlay!r}"
        raise SlackCatchUpBackendUnavailableError(message)
    identity = resolve_own_identity(backend)
    queues = QueuePaths.default()
    new_rows = 0

    def on_event(event_overlay: str, event: dict) -> None:
        nonlocal new_rows
        if record_dm(event_overlay, event):
            new_rows += 1

    def deliver(event: dict) -> bool:
        return deliver_event(
            overlay, event, context=DeliveryContext(queues=queues, identity=identity, on_event=on_event, seen=seen)
        )

    for item in catch_up(backend, alive_at=alive_at, deliver=deliver, overlay=overlay):
        seen.prune(before=item.oldest)
        if new_rows:
            wake()
            new_rows = 0
        yield item


@dataclass(slots=True)
class SlackCatchUpJob:
    overlay: str
    record: RecordDm
    wake: Wake
    name: str = field(default="slack_catchup", init=False)

    def scan(self) -> list[ScanSignal]:
        beat = read_heartbeat()
        if not heartbeat_is_stale(beat, self.overlay, now=time.time()):
            return []
        state = CatchUpState.load(self.overlay)
        evidence = [at for at in (beat.stamp_for(self.overlay) if beat else None, state.caught_up_at) if at]
        started = time.monotonic()
        for item in run_catch_up(
            self.overlay, alive_at=max(evidence, default=None), seen=state.seen, record_dm=self.record, wake=self.wake
        ):
            state.caught_up_at = item.caught_up_to
            state.save(self.overlay)
            if time.monotonic() - started >= JOB_BUDGET_SECONDS:
                break
        return []
