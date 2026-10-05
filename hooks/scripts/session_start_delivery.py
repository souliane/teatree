"""The ONE SessionStart write, and handing back what it carried when it never reached the session.

A starting session claims what it delivers — a parked hand-off, an answered question — before it
writes, so two sessions starting at once never both get it. The claim is the record of delivery, so
a write to a pipe the harness stopped reading lost it: the text was claimed, then dropped. Each claim
now registers how to hand itself back, the write goes through :func:`emit_additional_context` (it
returns only once the text reached the pipe), and a failed write hands every claim back for the next
start. Stdlib only; the hook still exits 0.
"""

import logging
from collections.abc import Callable

from hooks.scripts.additional_context import emit_additional_context

logger = logging.getLogger("teatree.hook_router")


class StartClaims:
    """What one SessionStart write carries, each with the way to hand it back."""

    def __init__(self) -> None:
        self._hand_backs: list[Callable[[], None]] = []

    def add(self, hand_back: Callable[[], None]) -> None:
        """Register how to hand back a claim this start's write carries."""
        self._hand_backs.append(hand_back)

    def deliver(self, context: str) -> None:
        """Write *context* as the start's one object (#1452's nested form; nothing for an empty merge, #256)."""
        if not context.strip():
            return
        try:
            emit_additional_context("SessionStart", context)
        except (OSError, ValueError):
            logger.warning("the SessionStart context did not reach the session; its claims are handed back")
            for hand_back in self._hand_backs:
                hand_back()


__all__ = ["StartClaims"]
