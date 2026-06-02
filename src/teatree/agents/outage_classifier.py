"""Classify a sub-agent death caused by a network/API outage (#1764).

When the harness loses its connection to the API mid-task, the sub-agent dies
and the result envelope it leaves behind often carries the connection error in
its ``summary`` or ``user_input_reason`` ("Unable to connect to API", "API
Error (Connection refused)", ...). The shared recorder chokepoint
:func:`teatree.agents.attempt_recorder.record_result_envelope` must not let
such a death advance the ticket FSM as a real completion — it has to land
FAILED. This module is the pure, side-effect-free classifier that chokepoint
consults.

Precision over recall: an outage death is rare relative to legit completions,
so a false positive (failing a genuine completion that merely *mentions* an API
error) is worse than a missed one. The verdict therefore keys on connection
signatures that do not occur in normal phase-completion prose, and treats the
bare phrase "API Error" as outage ONLY when it co-occurs with a connection
phrase — a summary like "added API error handling" is not an outage.
"""

from teatree.agents.result_schema import AgentResultBlob

_CONNECTION_SIGNATURES = (
    "unable to connect to api",
    "connectionrefused",
    "connection refused",
    "failedtoopensocket",
    "failed to open socket",
    "safety classifier unavailable",
)

_API_ERROR_PHRASE = "api error"

_CONNECTION_COOCCURRENCE_PHRASES = (
    "connect",
    "socket",
    "network",
    "timed out",
    "timeout",
    "unreachable",
    "refused",
    "reset by peer",
)


def _scan_text(result: AgentResultBlob, error: str) -> str:
    parts = [
        str(result.get("summary", "")),
        str(result.get("user_input_reason", "")),
        error,
    ]
    return " ".join(parts).casefold()


def is_outage_death(result: AgentResultBlob, *, error: str = "") -> bool:
    """Return whether *result* / *error* signal a network-outage sub-agent death.

    Case-insensitive across ``summary``, ``user_input_reason``, and the explicit
    ``error`` string. A direct connection signature is sufficient; the generic
    "API Error" phrase counts only when a connection phrase co-occurs, so a
    legitimate summary that merely discusses API-error handling is not flagged.
    """
    haystack = _scan_text(result, error)
    if any(signature in haystack for signature in _CONNECTION_SIGNATURES):
        return True
    if _API_ERROR_PHRASE in haystack:
        return any(phrase in haystack for phrase in _CONNECTION_COOCCURRENCE_PHRASES)
    return False
