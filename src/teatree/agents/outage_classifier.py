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
    return bool(outage_signature(result, error=error))


def outage_signature(result: AgentResultBlob, *, error: str = "") -> str:
    """Return the matched outage signature, or ``""`` when not an outage death.

    The signature is the diagnostic the recorder stamps onto the FAILED attempt
    (``error="outage_death: <sig>"``). A direct connection signature returns
    itself; the "API Error" path returns the co-occurring connection phrase.
    """
    haystack = _scan_text(result, error)
    for signature in _CONNECTION_SIGNATURES:
        if signature in haystack:
            return signature
    if _API_ERROR_PHRASE in haystack:
        for phrase in _CONNECTION_COOCCURRENCE_PHRASES:
            if phrase in haystack:
                return f"{_API_ERROR_PHRASE} + {phrase}"
    return ""


# Namespaced markers a FAILED attempt's ``error`` carries when the death was an
# infrastructure interruption rather than a deterministic defect. Each is emitted
# by exactly one recording seam: ``outage_death:`` by the recorder (#1764),
# ``result_error:`` by the headless driver for the #1764 "genuine FAILED run"
# class (a missing terminal ResultMessage OR an ``is_error`` result — both
# transient), ``provision_failed:`` by a worktree/provisioning step, and
# ``landing_unverified:`` by the completion chokepoint when a coder yielded
# without committing. A deterministic refusal (evidence gate, schema, review
# verdict, a real assertion/test failure, a ``stuck_loop`` runaway) matches none.
_TRANSIENT_MARKERS = (
    "outage_death:",
    "result_error:",
    "provision_failed:",
    "landing_unverified:",
)


def transient_failure_signature(error: str) -> str:
    """Return the transient signature of a FAILED attempt's *error*, or ``""``.

    A non-empty return means the failure was an infrastructure interruption the
    bounded auto-requeue sweep MAY reopen; ``""`` means a deterministic failure
    that must stay terminal FAILED. Keys on the namespaced markers above, plus a
    raw connection / "API Error + connection" signature in the error text (an
    outage death whose envelope was never stamped with the ``outage_death:``
    prefix). Case-insensitive.
    """
    haystack = error.casefold()
    if not haystack.strip():
        return ""
    for marker in _TRANSIENT_MARKERS:
        if marker in haystack:
            return marker.rstrip(": ")
    return outage_signature({}, error=error)


def is_transient_failure(error: str) -> bool:
    """Whether a FAILED attempt's *error* classifies as a transient interruption."""
    return bool(transient_failure_signature(error))
