"""Classify a sub-agent death caused by a network/API outage (#1764).

When the harness loses its connection to the API mid-task, the sub-agent dies
and the result envelope it leaves behind often carries the connection error in
its ``summary`` or ``user_input_reason`` ("Unable to connect to API", "API
Error (Connection refused)", ...). The shared recorder chokepoint
:func:`teatree.agents.attempt_recorder.record_result_envelope` must not let
such a death advance the ticket FSM as a real completion — it has to land
FAILED.

This module is the envelope-shaped face of that decision: it flattens the
envelope's prose into one haystack and hands it to
:func:`teatree.failure_signatures.outage_signature_in_text`, the dependency-free
leaf that carries the phrase tables and the precision-over-recall reasoning
behind the verdict.
"""

from teatree.agents.result_schema import AgentResultBlob
from teatree.failure_signatures import outage_signature_in_text


def _scan_text(result: AgentResultBlob, error: str) -> str:
    parts = [
        str(result.get("summary", "")),
        str(result.get("user_input_reason", "")),
        error,
    ]
    return " ".join(parts)


def outage_signature(result: AgentResultBlob, *, error: str = "") -> str:
    """Return the matched outage signature, or ``""`` when not an outage death.

    The signature is the diagnostic the recorder stamps onto the FAILED attempt
    (``error="outage_death: <sig>"``); an empty return means *result* / *error*
    are not an outage death. Scans ``summary``, ``user_input_reason``, and the
    explicit ``error`` string together, so a signature in any one of them counts.
    """
    return outage_signature_in_text(_scan_text(result, error))
