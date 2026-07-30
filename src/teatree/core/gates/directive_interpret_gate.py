"""Record a headless directive interpreter's returned envelope server-side (north-star PR-6).

The orchestrator half of the interpret lane, mirroring
``critic_gate.record_returned_critic_verdict``: a shell-denied interpreter RETURNS a
typed ``directive_interpretation`` envelope; THIS actor (not the one that captured
the text — maker≠checker) validates it deterministically and writes it onto the
``Directive``. The interpreter returns EITHER a ``sketch`` (recorded as a validated
:class:`~teatree.core.models.mechanism_sketch.MechanismSketch`, moving the directive
to ``INTERPRETED``) OR ``clarifying_questions`` when the directive is ambiguous
(recorded as single-use :class:`DeferredQuestion` rows, parking it in ``CLARIFYING``).

An envelope that fails the deterministic sketch gate returns an error string so the
caller FAILS the interpret task — the loop redispatches rather than record garbage.
A task with no dispatch row, or a result without the envelope, is a no-op (``""``).
"""

from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.core.models import DeferredQuestion, Directive, DirectiveError
from teatree.core.models.mechanism_sketch import MechanismSketchError, sketch_from_envelope
from teatree.core.overlay_loader import resolve_overlay_name

if TYPE_CHECKING:
    from teatree.core.models import Task

_ACTIVATION_ONLY = "activation_only"


def validate_activation_scope(raw_sketch: dict) -> str | None:
    """The registry half of sketch validation: the scope must resolve to an overlay.

    Lives in the gate (not the pure model) because it consults the overlay registry.
    An empty scope is a valid global mechanism; a non-empty scope that resolves to no
    registered overlay is refused — the structural checks are the model's.
    """
    scope = str(raw_sketch.get("activation_scope", "")).strip()
    if scope and resolve_overlay_name(scope) is None:
        return f"activation_scope {scope!r} does not resolve to a registered overlay"
    return None


def validate_setting_key(raw_sketch: dict) -> str | None:
    """For an ``activation_only`` sketch, the setting must already exist in the registry.

    An ``activation_only`` mechanism 'already exists' generically, so its ``setting_key``
    must resolve to a real ``UserSettings`` field — a bogus key passes the model's
    ``isidentifier`` check but would read-back-mismatch at CONFIGURE and park. A
    ``setting_policy_gate``'s setting is NEW (the implementation adds it), so it is not
    required to exist yet; the model's identifier check plus the configure read-back and
    the acceptance tests guard that case. Lives in the gate because it reads the settings
    registry, which the pure model layer must not import.
    """
    if str(raw_sketch.get("kind", "")).strip() != _ACTIVATION_ONLY:
        return None
    setting_key = str(raw_sketch.get("setting_key", "")).strip()
    if not hasattr(get_effective_settings(None), setting_key):
        return (
            f"setting_key {setting_key!r} is not a known setting; an activation_only mechanism "
            f"must reference an existing setting"
        )
    return None


def record_returned_directive_interpretation(task: "Task", result: dict) -> str:
    """Record an interpret task's returned ``directive_interpretation`` envelope.

    Returns ``""`` on success or a genuine no-op, or an error string the caller
    turns into a task failure (a malformed/unrecordable sketch, or a late result
    against a directive no longer awaiting interpretation).
    """
    dispatch = getattr(task, "directive_dispatches", None)
    dispatch_row = dispatch.first() if dispatch is not None else None
    if dispatch_row is None:
        return ""
    envelope = result.get("directive_interpretation")
    if not isinstance(envelope, dict):
        return ""
    directive = dispatch_row.directive
    questions = _clarifying_questions(envelope)
    if questions:
        return _record_clarifications(directive, questions)
    return _record_sketch(directive, envelope)


def _clarifying_questions(envelope: dict) -> list[str]:
    raw = envelope.get("clarifying_questions")
    if not isinstance(raw, list):
        return []
    return [q.strip() for q in raw if isinstance(q, str) and q.strip()]


def _record_clarifications(directive: Directive, questions: list[str]) -> str:
    """Record each clarifying question single-use and park the directive in ``CLARIFYING``."""
    for index, question in enumerate(questions):
        DeferredQuestion.record(
            f"Clarify directive #{directive.pk}: {question}",
            options_hash=f"directive_clarify:{directive.pk}:{directive.generation}:{index}",
        )
    try:
        directive.mark_clarifying()
    except DirectiveError as exc:
        return f"directive clarification refused: {exc}"
    return ""


def _record_sketch(directive: Directive, envelope: dict) -> str:
    """Validate the envelope's sketch and record it, moving the directive to ``INTERPRETED``."""
    raw_sketch = envelope.get("sketch")
    if not isinstance(raw_sketch, dict):
        return "directive interpretation carried neither a sketch nor clarifying_questions"
    scope_finding = validate_activation_scope(raw_sketch)
    if scope_finding is not None:
        return f"directive sketch recording refused: {scope_finding}"
    key_finding = validate_setting_key(raw_sketch)
    if key_finding is not None:
        return f"directive sketch recording refused: {key_finding}"
    try:
        sketch = sketch_from_envelope(raw_sketch)
    except MechanismSketchError as exc:
        return f"directive sketch recording refused: {exc}"
    constraint = str(envelope.get("constraint_statement") or "")
    try:
        directive.record_interpretation(sketch, constraint_statement=constraint)
    except DirectiveError as exc:
        return f"directive interpretation refused: {exc}"
    return ""
