from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.incoming_event import IncomingEvent


class HandlingOutcome(StrEnum):
    PROCESSED = "processed"
    RETRY = "retry"
    DEAD_LETTERED = "dead_lettered"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class EventHandler:
    source: str
    event_name: str
    loop: str
    handle: Callable[["IncomingEvent"], list[ScanSignal]]
    actions: frozenset[str] = frozenset()


ANY_ACTION = ""


class HandlerRegistry:
    def __init__(self) -> None:
        self._by_triple: dict[tuple[str, str, str], EventHandler] = {}

    def register(self, handler: EventHandler) -> None:
        triples = [(handler.source, handler.event_name, action) for action in handler.actions or {ANY_ACTION}]
        if taken := [triple for triple in triples if triple in self._by_triple]:
            msg = f"handler for {taken[0]} already registered"
            raise ValueError(msg)
        self._by_triple.update(dict.fromkeys(triples, handler))

    def lookup(self, source: str, event_name: str, action: str) -> EventHandler | None:
        specific = self._by_triple.get((source, event_name, action))
        return specific or self._by_triple.get((source, event_name, ANY_ACTION))

    def events(self, source: str) -> frozenset[str]:
        return frozenset(event_name for registered, event_name, _ in self._by_triple if registered == source)


HANDLERS = HandlerRegistry()
