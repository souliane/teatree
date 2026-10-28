"""Combine adjacent Slack messages into one answer turn."""

from dataclasses import dataclass, field

from teatree.core.models import PendingChatInjection
from teatree.loop.question_binding import _addressed
from teatree.loop.slack_answer.control_verbs import control_verb
from teatree.loop.slack_answer.priority_directive import parse_priority_directive

# Consecutive messages from the same user on the same channel, with no
# bot reply between them and received within this window, are one logical
# turn (a message + its quick follow-up). Zero-token: pure DB/time logic.
_COALESCE_WINDOW_SECONDS = 90


@dataclass(slots=True)
class _Unit:
    """One logical turn — one or more coalesced ``PendingChatInjection`` rows.

    The answer threads on the FIRST row's ``slack_ts``; every row in the
    unit gets the :eyes: receipt and is stamped ``loop_replied_at``
    together when the unit is replied to. ``text`` is the newline-joined
    message bodies in original (received) order — a single unit for the
    classifier and the answer builder.
    """

    rows: list[PendingChatInjection] = field(default_factory=list)

    @property
    def lead(self) -> PendingChatInjection:
        return self.rows[0]

    @property
    def channel(self) -> str:
        return self.lead.channel

    @property
    def slack_ts(self) -> str:
        return self.lead.slack_ts

    @property
    def overlay(self) -> str:
        return self.lead.overlay

    @property
    def text(self) -> str:
        return "\n".join(r.text for r in self.rows)


def _coalesce(rows: list[PendingChatInjection]) -> list[_Unit]:
    """Group consecutive same-user/channel rows into logical turns.

    A new unit starts when the next row is from a different
    ``(overlay, channel, user_id)``, sits in a different Slack thread, OR its
    ``received_at`` is more than :data:`_COALESCE_WINDOW_SECONDS` after the
    previous row's, OR either row is only a control word (pause / resume), which is
    never continued onto an earlier row nor by a later one. Rows arrive oldest-first
    (``loop_unreplied()`` ordering).
    The loop is the only bot that replies and it stamps ``loop_replied_at`` on
    a whole unit at once, so every row reaching this function is pre-reply —
    "no bot message between" reduces to the same-actor/same-thread/within-window
    adjacency test.
    """
    units: list[_Unit] = []
    for row in rows:
        if units and _continues(units[-1].rows[-1], row):
            units[-1].rows.append(row)
        else:
            units.append(_Unit(rows=[row]))
    return units


def _acts_alone(text: str) -> bool:
    """A priority directive, or a message that is only pause / resume: joined to a neighbour it stops being one."""
    return parse_priority_directive(text) is not None or control_verb(text) is not None


def _continues(prev: PendingChatInjection, nxt: PendingChatInjection) -> bool:
    """True iff *nxt* continues *prev* (same actor, same thread, within window)."""
    if (prev.overlay, prev.channel, prev.user_id) != (nxt.overlay, nxt.channel, nxt.user_id):
        return False
    if _acts_alone(prev.text) or _acts_alone(nxt.text):
        return False
    if prev.thread_ts != nxt.thread_ts:
        # Two threads are two conversations: a unit binds on its LEAD's thread,
        # so coalescing across them feeds one question the other's answer.
        return False
    if _addressed(nxt.text) is not None:
        return False
    if not nxt.user_id:
        # No user attribution → cannot prove same author; never coalesce.
        return False
    gap = (nxt.received_at - prev.received_at).total_seconds()
    return 0 <= gap <= _COALESCE_WINDOW_SECONDS
