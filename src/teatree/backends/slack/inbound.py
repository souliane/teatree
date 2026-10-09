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

    The batch a tick observes is FROZEN at the tick's FIRST :meth:`snapshot`.
    Every scanner that shares the backend then reads the identical batch even
    when an event arrives between two scanners' snapshots — the earlier clear
    point (the first :meth:`enqueue` after any :meth:`snapshot`) rolled the
    batch mid-tick, so an event that arrived between two scanners was seen by
    only the later one while the earlier one had already read the pre-roll
    batch. A mid-tick arrival is now buffered for the NEXT tick and never
    changes what this tick's scanners see.

    :meth:`begin_tick` rolls the buffered next-tick arrivals in as the new
    frozen batch — the tick driver calls it once at the start of each tick,
    before the scanners run. Between ticks the frozen batch is re-served
    idempotently (the consuming scanners dedup on Slack ``ts`` / ``event_ts``
    in their persistence layer), so a missed :meth:`begin_tick` degrades to a
    stale re-serve, never a cross-scanner disagreement.
    """

    def __init__(self) -> None:
        # ``_current`` is the frozen batch this tick's scanners read; ``_incoming``
        # accumulates arrivals that land AFTER the batch was frozen, to be rolled
        # in by the next :meth:`begin_tick`. ``_frozen`` flips on the first
        # :meth:`snapshot` so a later arrival cannot mutate the served batch.
        self._current: list[RawAPIDict] = []
        self._incoming: list[RawAPIDict] = []
        self._frozen = False
        self._lock = threading.Lock()

    def enqueue(self, event: RawAPIDict) -> None:
        with self._lock:
            if self._frozen:
                self._incoming.append(event)
            else:
                self._current.append(event)

    def snapshot(self) -> list[RawAPIDict]:
        with self._lock:
            self._frozen = True
            return list(self._current)

    def begin_tick(self) -> None:
        with self._lock:
            self._current = self._incoming
            self._incoming = []
            self._frozen = False


class SlackInbound:
    """Socket Mode inbound ingestion for one backend.

    The Phase 3.6 Socket Mode receiver pushes ``app_mention`` /
    ``message.im`` / ``reaction_added`` events through :meth:`enqueue_mention`
    / :meth:`enqueue_dm` / :meth:`enqueue_reaction`; the loop scanners read
    each per-tick batch through :meth:`snapshot_mentions` / :meth:`snapshot_dms`
    / :meth:`snapshot_reactions`. Reads are non-destructive within a tick so
    the scanners that share one backend each observe the same batch, frozen at
    the tick's first snapshot (#1655). The tick driver calls :meth:`begin_tick`
    once at the start of each tick to roll in the arrivals that were buffered
    for the next tick across all three queues at once.
    Bundling the three queues and their ingestion behind one collaborator
    keeps the inbound concern out of the outbound messaging surface.
    """

    def __init__(self) -> None:
        self._mentions = _TickFanoutQueue()
        self._dms = _TickFanoutQueue()
        self._reactions = _TickFanoutQueue()

    def begin_tick(self) -> None:
        """Freeze the next per-tick batch across all three queues (mentions/DMs/reactions)."""
        self._mentions.begin_tick()
        self._dms.begin_tick()
        self._reactions.begin_tick()

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
