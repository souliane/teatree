"""Record a quarantined reader's returned candidate server-side, fail-closed (#116, Layer 3).

The orchestrator half of the context firewall, mirroring
``directive_interpret_gate.record_returned_directive_interpretation``: the no-tools/
no-creds reader RETURNS a typed ``directive_candidate`` envelope, and THIS actor (a
different one than the quarantined reader — maker≠checker) validates it against the
TRUE source event and mints the ``Directive`` only when it passes.

Fail-closed is the whole point: on ANY finding — a provenance the reader tried to
upgrade, or a Layer-2 structural / injection finding — this returns a non-empty error
and writes ZERO ``Directive`` rows. The raw attacker text stays inert on
``IncomingEvent.body``; only a schema-validated, length-capped ``normalized_constraint``
ever becomes ``Directive.raw_text``, so every downstream tooled stage is safe by
construction (it works from sanitized text, never raw content).
"""

from teatree.core.models import Directive, IncomingEvent
from teatree.core.models.directive_candidate import DirectiveCandidateError, candidate_from_envelope


def record_returned_directive_candidate(source_event: IncomingEvent, result: dict) -> str:
    """Record a reader task's returned ``directive_candidate`` envelope against *source_event*.

    Returns ``""`` on success or a genuine no-op (no envelope), or a non-empty error
    the caller turns into a task failure (a provenance mismatch, or a structural /
    injection finding) — in which case NO ``Directive`` is minted.

    The source event is passed by the trusted caller, NEVER read from the envelope, so
    the reader cannot point the taint at a different event: the provenance cross-check
    compares the reader's ECHOED ``provenance`` against ``source_event.provenance`` and
    rejects a mismatch (the reader cannot upgrade its own trust). The minted
    directive's taint is derived from the true event by ``Directive.capture``,
    independent of anything the reader emitted.
    """
    envelope = result.get("directive_candidate")
    if not isinstance(envelope, dict):
        return ""
    echoed = str(envelope.get("provenance") or "").strip()
    if echoed and echoed != source_event.provenance:
        return (
            f"directive candidate refused: echoed provenance {echoed!r} does not match the source "
            f"event's {source_event.provenance!r} — the reader cannot upgrade its own trust"
        )
    try:
        candidate = candidate_from_envelope(envelope)
    except DirectiveCandidateError as exc:
        return f"directive candidate refused: {exc}"
    Directive.objects.capture(
        candidate.normalized_constraint,
        source=Directive.Source.INCOMING_EVENT,
        scope_overlay=candidate.scope_overlay,
        source_event=source_event,
    )
    return ""
