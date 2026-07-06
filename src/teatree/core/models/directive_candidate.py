"""The typed, length-capped candidate a quarantined reader emits (#116, Layer 2).

The context firewall's second layer: the no-tools/no-creds reader profile
(:mod:`teatree.agents.reader_profile`) that ingests untrusted content emits ONLY a
:class:`DirectiveCandidate` — a structured, single-statement, length-capped verdict,
never free-form text. This module is the schema, mirroring
:mod:`teatree.core.models.mechanism_sketch` exactly: a ``TypedDict`` wire shape, a
frozen dataclass, a deterministic first-finding-wins ``validate_candidate_structure``,
and a raising ``candidate_from_envelope`` writer path.

:func:`validate_candidate_structure` is the STRUCTURAL half; the server-side recorder
gate (:mod:`teatree.core.gates.directive_candidate_gate`, Layer 3) layers the
provenance cross-check on top (it needs the source event the pure model layer must not
touch) and mints the ``Directive`` only when both pass — fail-closed, so a malformed or
injection-laden emission writes ZERO rows and the raw attacker text stays inert.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

#: The hard cap on a candidate's normalized constraint. A directive about teatree's
#: own behaviour is one short sentence; anything longer is a passed-through payload,
#: not a sanitized constraint, and is refused.
MAX_CONSTRAINT_LEN = 500

#: Characters below this ordinal are control chars (newlines, tabs, escapes) — a
#: sanitized single-line constraint carries none.
_CONTROL_CHAR_CEILING = 32

#: Classic prompt-injection tells a well-behaved reader must never pass through into a
#: normalized constraint — a bounded, lowercase substring denylist. Their presence
#: means the reader echoed attacker text rather than a sanitized verdict, so the
#: candidate is refused (Layer 2 of the dual-LLM boundary).
_INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "disregard all",
    "system prompt",
    "you are now",
    "new instructions",
    "override instructions",
    "post to #",
)


class DirectiveCandidateDict(TypedDict, total=False):
    """The JSON wire shape a reader emits — canonical here (the model layer).

    ``teatree.agents.result_schema`` imports it for the ``directive_reading``
    envelope. All keys optional because the recorder validates a possibly-malformed
    hand-back before it becomes a candidate. ``provenance`` is the reader's ECHOED
    trust tag — the recorder cross-checks it against the true source event and never
    trusts it as the taint source.
    """

    is_directive: bool
    normalized_constraint: str
    scope_overlay: str
    cited_signal: str
    provenance: str


class DirectiveCandidateError(ValueError):
    """Raised when a reader envelope cannot be recorded as a valid candidate."""


@dataclass(frozen=True, slots=True)
class DirectiveCandidate:
    """One reader's sanitized verdict — a single-statement, length-capped constraint."""

    is_directive: bool
    normalized_constraint: str
    scope_overlay: str = ""
    cited_signal: str = ""
    #: The reader's echoed trust tag. Cross-checked by the recorder against the true
    #: source event; the directive's taint is derived from the EVENT, never this.
    provenance: str = ""

    @classmethod
    def from_dict(cls, raw: DirectiveCandidateDict) -> "DirectiveCandidate":
        """Rebuild a candidate from its wire dict (validation is the caller's job)."""
        return cls(
            is_directive=bool(raw.get("is_directive")),
            normalized_constraint=str(raw.get("normalized_constraint", "")).strip(),
            scope_overlay=str(raw.get("scope_overlay", "")).strip(),
            cited_signal=str(raw.get("cited_signal", "")).strip(),
            provenance=str(raw.get("provenance", "")).strip(),
        )


def _check_is_directive(raw: DirectiveCandidateDict) -> str | None:
    if raw.get("is_directive") is True:
        return None
    return "is_directive must be True — a non-directive verdict mints nothing"


def _check_constraint_present(raw: DirectiveCandidateDict) -> str | None:
    if str(raw.get("normalized_constraint", "")).strip():
        return None
    return "normalized_constraint is required and must be non-empty"


def _check_constraint_length(raw: DirectiveCandidateDict) -> str | None:
    constraint = str(raw.get("normalized_constraint", ""))
    if len(constraint) > MAX_CONSTRAINT_LEN:
        return f"normalized_constraint exceeds the {MAX_CONSTRAINT_LEN}-char cap (len={len(constraint)})"
    return None


def _check_single_line(raw: DirectiveCandidateDict) -> str | None:
    constraint = str(raw.get("normalized_constraint", ""))
    if any(ord(char) < _CONTROL_CHAR_CEILING for char in constraint):
        return "normalized_constraint must be a single line (no control chars / newlines)"
    return None


def _check_no_code_fence(raw: DirectiveCandidateDict) -> str | None:
    if "`" in str(raw.get("normalized_constraint", "")):
        return "normalized_constraint must not contain code fences or backticks"
    return None


def _check_no_injection_marker(raw: DirectiveCandidateDict) -> str | None:
    lowered = str(raw.get("normalized_constraint", "")).lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            return f"normalized_constraint contains an injection marker ({marker!r})"
    return None


def _check_scope_overlay(raw: DirectiveCandidateDict) -> str | None:
    scope = str(raw.get("scope_overlay", "")).strip()
    if scope and not all(char.isalnum() or char in "-_" for char in scope):
        return f"scope_overlay {scope!r} is not a valid overlay identifier"
    return None


#: The STRUCTURAL candidate checks, applied in order — first finding wins (mirrors
#: ``validate_sketch_structure``): a real directive verdict, a non-empty constraint
#: within the length cap, a single line with no code fence / injection marker, and a
#: well-formed overlay scope. The provenance cross-check is the recorder gate's (it
#: needs the source event the pure model layer must not import).
_STRUCTURE_CHECKS: tuple[Callable[[DirectiveCandidateDict], str | None], ...] = (
    _check_is_directive,
    _check_constraint_present,
    _check_constraint_length,
    _check_single_line,
    _check_no_code_fence,
    _check_no_injection_marker,
    _check_scope_overlay,
)


def validate_candidate_structure(raw: DirectiveCandidateDict) -> str | None:
    """Return the first STRUCTURAL finding if *raw* is not a recordable candidate, else ``None``.

    Each check is fail-loud with a named reason so a rejected candidate is auditable
    (see :data:`_STRUCTURE_CHECKS`). The provenance cross-check is layered on by the
    recorder gate.
    """
    for check in _STRUCTURE_CHECKS:
        finding = check(raw)
        if finding is not None:
            return finding
    return None


def candidate_from_envelope(raw: DirectiveCandidateDict) -> DirectiveCandidate:
    """Validate structure then build a :class:`DirectiveCandidate`, raising on any finding.

    The single structural writer path: :func:`validate_candidate_structure` first (so a
    structurally-invalid or injection-laden envelope never becomes a candidate), then
    :meth:`DirectiveCandidate.from_dict`. The recorder gate applies the provenance
    cross-check separately, before minting a ``Directive``.
    """
    finding = validate_candidate_structure(raw)
    if finding is not None:
        raise DirectiveCandidateError(finding)
    return DirectiveCandidate.from_dict(raw)
