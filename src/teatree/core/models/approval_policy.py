"""The taint-floor approval seam — an untrusted action can never AUTO_APPROVE (#116).

The single pure function every future "may this act autonomously?" caller consults.
Its load-bearing property is the ORDER of its two checks: the taint FLOOR is evaluated
FIRST and short-circuits, so a future permissive ``dial`` (PR-11) can widen the
owner-taint path but can never reach — let alone override — an untrusted-taint action.
A non-owner taint is ASK, full stop.

#116 ships the empty dial (:func:`_ask_everything`, always ASK), so the seam is
structurally present and pinned by tests while behaviour stays byte-identical to
today: every directive still requires a human. PR-11 injects a real per-action-class
dial; it changes only the owner-taint branch, never the floor.
"""

from collections.abc import Callable
from enum import StrEnum

from teatree.core.models.provenance import Provenance

type Dial = Callable[[str], Decision]


class Decision(StrEnum):
    """The approval verdict for one action: ask a human, or proceed autonomously."""

    ASK = "ask"
    AUTO_APPROVE = "auto_approve"


def _ask_everything(_action_class: str) -> Decision:
    """The #116 dial: every action class asks a human (the empty, always-ASK dial)."""
    return Decision.ASK


def approval_policy(action_class: str, taint: str, *, dial: Dial | None = None) -> Decision:
    """Return the approval :class:`Decision` for *action_class* under *taint*.

    The taint FLOOR is the first branch and short-circuits: any taint other than
    :attr:`Provenance.OWNER` — public, web, colleague, blank, an unknown string —
    returns :attr:`Decision.ASK` BEFORE *dial* is ever consulted, so no dial can
    auto-approve an untrusted action. Only an owner-taint action reaches the dial
    (:func:`_ask_everything` in #116 — still ASK). This is the whole security
    guarantee: floor-first, never dial-first.
    """
    if taint != Provenance.OWNER:
        return Decision.ASK
    return (dial or _ask_everything)(action_class)
