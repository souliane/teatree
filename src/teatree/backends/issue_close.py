"""Read a forge issue payload for the one fact "did the forge close this issue?" (#4711).

A pre-ship ticket is the factory's claim that work is still to do; its issue being
closed is the owner's statement that it is not. Every other board-reconcile rule
resolves a ticket's URL as a PULL REQUEST, so a pre-ship ticket whose ``issue_url``
names an ISSUE the forge closed reached no rule at all and stayed a dispatch source
forever — measured at eleven identical re-dispatches of one ticket.

The close REASON rides along because ``completed`` and ``not_planned`` are different
owner decisions the board must stay able to tell apart after the retirement. GitHub
supplies it as ``state_reason``; no other forge teatree speaks to marks one, so an
absent reason is normal and never weakens the CLOSED verdict.
"""

from dataclasses import dataclass

from teatree.backends.issue_payload import payload_or_none
from teatree.core.backend_protocols import IssueOpenState
from teatree.types import RawAPIDict

#: Issue states that mean closed — the same set ``OverlayBase.is_issue_done`` reads, so
#: the two never disagree about which payloads are settled.
_CLOSED_STATES = frozenset({"closed", "completed"})
#: GitLab says ``opened`` where GitHub says ``open``; anything else is not a state
#: teatree can read a verdict from.
_OPEN_STATES = frozenset({"open", "opened"})

_STATE_REASON_KEY = "state_reason"


@dataclass(frozen=True, slots=True)
class IssueCloseVerdict:
    """What the forge says about one issue's open/closed state, and why it closed."""

    state: IssueOpenState
    reason: str = ""


#: The fail-CLOSED answer every unreadable shape and every failed fetch collapses to.
UNKNOWN_VERDICT = IssueCloseVerdict(IssueOpenState.UNKNOWN)


def close_verdict_from_payload(issue_data: object) -> IssueCloseVerdict:
    """Classify a raw forge issue payload as CLOSED / OPEN / UNKNOWN.

    Pure and forge-agnostic: the caller owns the fetch and its failure modes, so this
    never raises. Every shape it cannot positively classify — a non-dict, an error
    envelope, a missing or non-string ``state``, a state no forge teatree speaks to
    uses — collapses to UNKNOWN, because retiring a ticket on a guess is the failure
    this path exists to prevent.
    """
    payload = payload_or_none(issue_data)
    if payload is None:
        return UNKNOWN_VERDICT
    state = payload.get("state")
    if not isinstance(state, str):
        return UNKNOWN_VERDICT
    normalized = state.lower()
    if normalized in _CLOSED_STATES:
        return IssueCloseVerdict(IssueOpenState.CLOSED, _close_reason(payload))
    return IssueCloseVerdict(IssueOpenState.OPEN) if normalized in _OPEN_STATES else UNKNOWN_VERDICT


def _close_reason(payload: RawAPIDict) -> str:
    reason = payload.get(_STATE_REASON_KEY)
    return reason.lower() if isinstance(reason, str) else ""
