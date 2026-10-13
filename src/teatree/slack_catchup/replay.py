"""The catch-up pass: compute the window, read it whole, then hand every item to *deliver*."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.self_identity import resolve_own_identity
from teatree.slack_catchup.dm import owner_dm_events
from teatree.slack_catchup.window import SLICE_SECONDS, CatchUpRefusedError, CatchUpWindow
from teatree.types import RawAPIDict

type Deliver = Callable[[RawAPIDict], bool]


@dataclass(frozen=True, slots=True)
class CatchUpSlice:
    caught_up_to: float
    oldest: float
    delivered: int


def catch_up(backend: SlackBotBackend, *, alive_at: float | None, deliver: Deliver) -> Iterator[CatchUpSlice]:
    """Deliver every item the window holds; raise, delivering nothing, when any read is refused.

    *alive_at* is when the listener was last known alive; ``None`` means no evidence,
    which walks the whole capped window.
    """
    window = CatchUpWindow.since(alive_at, now=time.time())
    identity = resolve_own_identity(backend)
    if identity is None:
        msg = "the bot's own Slack identity is unresolved"
        raise CatchUpRefusedError(msg)
    start = window.start
    while start < window.end:
        end = min(start + SLICE_SECONDS, window.end)
        slice_window = CatchUpWindow(start=start, oldest=start - (window.start - window.oldest), end=end)
        events = owner_dm_events(backend, window=slice_window, identity=identity)
        delivered = sum(1 for event in events if deliver(event))
        yield CatchUpSlice(caught_up_to=end, oldest=slice_window.oldest, delivered=delivered)
        start = end
