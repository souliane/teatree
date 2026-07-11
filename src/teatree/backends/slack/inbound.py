"""Socket Mode inbound ingestion for ``SlackBotBackend`` (#1655).

The per-tick inbound event buffers, split out of ``bot.py`` so the backend's
outbound messaging surface stays under the module-health LOC cap. The Phase 3.6
Socket Mode receiver pushes ``app_mention`` / ``message.im`` / ``reaction_added``
events into :class:`SlackInbound`; the loop scanners read each per-tick batch
back non-destructively so the scanners that share one backend each observe the
same batch instead of racing a destructive drain.
"""

import threading

from teatree.types import RawAPIDict


class _TickFanoutQueue:
    """Thread-safe inbound event buffer read non-destructively within a tick.

    The Socket Mode receiver calls :meth:`enqueue`; every scanner that
    shares one backend calls :meth:`snapshot` in the same tick. A
    destructive drain would let whichever scanner runs first consume the
    batch and leave the others with nothing — for DMs and reactions that
    means the RED CARD scanner falls back to degraded polling and can miss
    a real signal (#1655). :meth:`snapshot` instead returns a copy, so each
    of the concurrently-scheduled scanners observes the same events.

    The defined clear point is the first :meth:`enqueue` after any
    :meth:`snapshot`: a fresh event begins a new tick's batch and drops the
    already-served one, bounding the buffer at one tick's worth of events.
    Re-serving the same batch across consecutive no-arrival ticks is
    idempotent — the consuming scanners dedup on Slack ``ts`` / ``event_ts``
    in their persistence layer.
    """

    def __init__(self) -> None:
        self._events: list[RawAPIDict] = []
        self._served = False
        self._lock = threading.Lock()

    def enqueue(self, event: RawAPIDict) -> None:
        with self._lock:
            if self._served:
                self._events = []
                self._served = False
            self._events.append(event)

    def snapshot(self) -> list[RawAPIDict]:
        with self._lock:
            self._served = True
            return list(self._events)


class SlackInbound:
    """Socket Mode inbound ingestion for one backend.

    The Phase 3.6 Socket Mode receiver pushes ``app_mention`` /
    ``message.im`` / ``reaction_added`` events through :meth:`enqueue_mention`
    / :meth:`enqueue_dm` / :meth:`enqueue_reaction`; the loop scanners read
    each per-tick batch through :meth:`snapshot_mentions` / :meth:`snapshot_dms`
    / :meth:`snapshot_reactions`. Reads are non-destructive within a tick so
    the scanners that share one backend each observe the same batch (#1655).
    Bundling the three queues and their ingestion behind one collaborator
    keeps the inbound concern out of the outbound messaging surface.
    """

    def __init__(self) -> None:
        self._mentions = _TickFanoutQueue()
        self._dms = _TickFanoutQueue()
        self._reactions = _TickFanoutQueue()

    def enqueue_mention(self, event: RawAPIDict) -> None:
        self._mentions.enqueue(event)

    def enqueue_dm(self, event: RawAPIDict) -> None:
        self._dms.enqueue(event)

    def enqueue_reaction(self, event: RawAPIDict) -> None:
        self._reactions.enqueue(event)

    def snapshot_mentions(self) -> list[RawAPIDict]:
        return self._mentions.snapshot()

    def snapshot_dms(self) -> list[RawAPIDict]:
        return self._dms.snapshot()

    def snapshot_reactions(self) -> list[RawAPIDict]:
        return self._reactions.snapshot()
