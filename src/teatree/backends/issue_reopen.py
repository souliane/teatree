"""Read a forge issue payload for the one fact "was this closed and then reopened?" (#4152).

A ticket owns its issue URL in every state but IGNORED, so a DELIVERED ticket owns
its issue forever and intake can never re-admit it. Reviving on "the issue is open"
would sweep up every delivered ticket whose issue merely never closed and re-do work
that already shipped, so the release has to turn on the narrower REOPENED fact.

GitHub's REST issue payload answers it directly: ``state_reason`` is ``"reopened"``
after a reopen and ``null`` on an issue that never closed. No other forge teatree
speaks to marks it on the issue object, so a payload carrying no ``state_reason``
key is :attr:`~teatree.core.backend_protocols.IssueReopenState.UNKNOWN` — the whole
point of the third value is that a missing marker is never read as "not reopened".
"""

from typing import cast

from teatree.core.backend_protocols import IssueReopenState
from teatree.types import RawAPIDict

#: Issue states that mean done — the same set ``OverlayBase.is_issue_done`` reads, so
#: the two never disagree about which payloads are settled.
_DONE_STATES = frozenset({"closed", "completed"})

_STATE_REASON_KEY = "state_reason"
_REOPENED_REASON = "reopened"


def reopen_state_from_payload(issue_data: object) -> IssueReopenState:
    """Classify a raw forge issue payload as REOPENED / NOT_REOPENED / UNKNOWN.

    Pure and forge-agnostic: the caller owns the fetch and its failure modes, so
    this never raises. Every shape it cannot positively classify — a non-dict, an
    ``{"error": ...}`` envelope, a missing or non-string ``state``, an open issue
    with no ``state_reason`` key — collapses to UNKNOWN.
    """
    payload = _as_payload(issue_data)
    if payload is None:
        return IssueReopenState.UNKNOWN
    state = payload.get("state")
    if not isinstance(state, str):
        return IssueReopenState.UNKNOWN
    if state.lower() in _DONE_STATES:
        return IssueReopenState.NOT_REOPENED
    return _from_state_reason(payload)


def _as_payload(issue_data: object) -> RawAPIDict | None:
    """The issue payload itself, or ``None`` for a shape that carries no verdict at all."""
    if not isinstance(issue_data, dict):
        return None
    payload = cast("RawAPIDict", issue_data)
    return None if "error" in payload else payload


def _from_state_reason(payload: RawAPIDict) -> IssueReopenState:
    """An OPEN issue's verdict, which only its ``state_reason`` marker can supply."""
    if _STATE_REASON_KEY not in payload:
        return IssueReopenState.UNKNOWN
    reason = payload[_STATE_REASON_KEY]
    if isinstance(reason, str) and reason.lower() == _REOPENED_REASON:
        return IssueReopenState.REOPENED
    return IssueReopenState.NOT_REOPENED
