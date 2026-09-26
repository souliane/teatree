"""Pure plan-adequacy validators (SELFCATCH-3) — the structural anti-thin-spec check.

The named root cause of the 26-bug integration campaign was thin-spec-as-plan: a
``PlanArtifact`` satisfied the plan gate on ``plan_text.strip()`` alone, so a
scope+acceptance spec passed as a plan and a coder was dispatched with nothing
naming the seams the change touched. These pure functions are the structural
substitute — the five-section manifest each real plan must carry.

A leaf module (no DB, no gate imports) so both the model (``plan_artifact``,
which enforces the manifest at ``record`` time) and the gate
(``plan_currency_gate``, which re-checks it and reads the declared seams) import
DOWN to it — never a model→gate up-edge. Mirrors ``anti_vacuity_gate.is_complete``:
each section is substantive OR an explicit reasoned negative, and silence never
passes.
"""

import string
from typing import TYPE_CHECKING, cast

from teatree.core.models.mechanism_sketch import MechanismSketch, is_core_seam_chokepoint
from teatree.core.models.types import AdequacySection, PlanAdequacy

if TYPE_CHECKING:
    from collections.abc import Mapping

# The five sections every real plan must speak to. Missing any one is a thin spec.
REQUIRED_ADEQUACY_SECTIONS: tuple[str, ...] = (
    "design",
    "integration_seams",
    "edge_cases",
    "test_strategy",
    "acceptance_criteria",
)

#: The section that is also a PRODUCER: its content becomes the ticket's ``core.Rubric``
#: criteria, so the plan is the single home for the acceptance-criteria list.
ACCEPTANCE_CRITERIA_SECTION: str = "acceptance_criteria"

# The extra section a DIRECTIVE-linked ticket's plan must also declare (north-star
# PR-5): the generic-shape decision, structured so ``mechanism_conforms`` checks it
# against the ratified sketch deterministically (its own presence check is that
# enforcement — the five required sections still go through ``is_adequate``).
MECHANISM_PLACEMENT_SECTION: str = "mechanism_placement"

_SHA_LEN = 40


def is_valid_base_sha(base_sha: object) -> bool:
    """Whether *base_sha* is a full 40-char hex commit SHA (the plan's authored base)."""
    if not isinstance(base_sha, str):
        return False
    cleaned = base_sha.strip()
    return len(cleaned) == _SHA_LEN and all(c in string.hexdigits for c in cleaned)


def _section_content(section: "Mapping[str, object]") -> tuple[str, ...]:
    """One section's ``content`` as non-blank strings — the single answer to "does it speak?"."""
    content = section.get("content")
    if isinstance(content, list):
        return tuple(str(item).strip() for item in content if str(item).strip())
    if isinstance(content, str) and content.strip():
        return (content.strip(),)
    return ()


def _stated_reason(section: "Mapping[str, object]") -> bool:
    """Whether the section carries a non-blank ``none_reason``."""
    reason = section.get("none_reason")
    return isinstance(reason, str) and bool(reason.strip())


def section_complete(section: object) -> bool:
    """Whether one adequacy *section* is substantive OR carries an explicit reasoned negative.

    Substantive = non-empty text, or a list with at least one non-blank item.
    Otherwise a non-blank ``none_reason`` (the explicit reasoned negative) satisfies
    it. Both empty — the forgotten/silent section — never passes.
    """
    if not isinstance(section, dict):
        return False
    fields = cast("Mapping[str, object]", section)
    return bool(_section_content(fields)) or _stated_reason(fields)


def is_adequate(adequacy: object) -> bool:
    """Whether *adequacy* is a complete five-section manifest.

    Every required section must be present and complete (:func:`section_complete`).
    A scope+acceptance-only thin spec — no seams/edge-cases/test-strategy claims —
    fails here, which is the whole point.
    """
    if not isinstance(adequacy, dict):
        return False
    fields = cast("Mapping[str, object]", adequacy)
    return all(section_complete(fields.get(name)) for name in REQUIRED_ADEQUACY_SECTIONS)


def _declared_content(adequacy: object, section_name: str) -> tuple[str, ...]:
    """One section's ``content`` as non-blank strings; ``()`` for a negative or malformed one."""
    if not isinstance(adequacy, dict):
        return ()
    section = cast("Mapping[str, object]", adequacy).get(section_name)
    if not isinstance(section, dict):
        return ()
    return _section_content(cast("Mapping[str, object]", section))


def declared_seam_paths(adequacy: object) -> tuple[str, ...]:
    """The registries/contracts/paths the plan declares it touches (its integration seams).

    An explicit ``no_seams`` negative (``none_reason`` set, no content) yields ``()`` — the
    plan claims no seams, so the currency gate finds nothing to guard (a diff that
    later touches a known seam is the DEFERRED post-diff seam-parity checker's job,
    not this one's). Empty on a missing/malformed manifest.
    """
    return _declared_content(adequacy, "integration_seams")


def declared_acceptance_criteria(adequacy: object) -> tuple[str, ...]:
    """The acceptance criteria the plan declares — the texts its ``core.Rubric`` is built from."""
    return _declared_content(adequacy, ACCEPTANCE_CRITERIA_SECTION)


def negated_section(reason: str) -> AdequacySection:
    """An explicit reasoned-negative section — the ``no_seams: {reason}`` shape."""
    return AdequacySection(none_reason=reason)


def all_negated_adequacy(reason: str) -> PlanAdequacy:
    """A minimal-but-VALID manifest, every section an explicit reasoned negative.

    The shape ONLY ``PlanArtifact.record_bypass`` writes: it passes :func:`is_adequate`
    (no section is silent) while asserting the change touches no seams, has no edge
    cases, adds no tests, and declares no acceptance criteria — all with the same
    stated *reason*. ``record`` refuses it, so a plan of this shape always carries a
    recorded human authorization.
    """
    return PlanAdequacy(
        design=AdequacySection(content=reason),
        integration_seams=negated_section(reason),
        edge_cases=negated_section(reason),
        test_strategy=negated_section(reason),
        acceptance_criteria=negated_section(reason),
    )


# --------------------------------------------------------------------------- #
# mechanism_placement (north-star PR-5) — the directive-scoped extra section: the
# plan's recorded generic-shape decision, checked against the ratified sketch.
# --------------------------------------------------------------------------- #
def _mechanism_placement_declaration(adequacy: object) -> "Mapping[str, object]":
    """The ``mechanism_placement`` section's declared fields, or an empty mapping."""
    if not isinstance(adequacy, dict):
        return {}
    section = cast("Mapping[str, object]", adequacy).get(MECHANISM_PLACEMENT_SECTION)
    return cast("Mapping[str, object]", section) if isinstance(section, dict) else {}


def _declared_str_list(value: object) -> tuple[str, ...]:
    """Normalise a declared JSON list (or lone string) to non-blank strings."""
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _is_reasoned_negative(section: "Mapping[str, object]") -> bool:
    """A stated reason AND no content — a section that declares something is not a negative."""
    return _stated_reason(section) and not _section_content(section)


def is_plan_bypass_shaped(adequacy: object) -> bool:
    """Whether *adequacy* is an all-reasoned-negative bypass manifest (:func:`all_negated_adequacy`).

    Every seam/edge/test section is a reasoned negative with no content — the shape only
    ``PlanArtifact.record_bypass`` writes, so this predicate IS "a human authorized this
    plan-bypass". It is the single waiver signal: ``mechanism_conforms`` waives such a
    plan (never-lockout on a directive ticket) and the rubric gate waives it too.
    The no-content half is load-bearing: the schema admits both keys, so a substantive
    manifest a planner also annotated with reasons would otherwise read as the waiver.
    """
    if not isinstance(adequacy, dict):
        return False
    fields = cast("Mapping[str, object]", adequacy)
    non_design = tuple(name for name in REQUIRED_ADEQUACY_SECTIONS if name != "design")
    return all(
        isinstance(fields.get(name), dict) and _is_reasoned_negative(cast("Mapping[str, object]", fields.get(name)))
        for name in non_design
    )


def mechanism_conforms(adequacy: object, sketch: MechanismSketch) -> str | None:
    """A finding if the plan's ``mechanism_placement`` drifts from the ratified *sketch*, else ``None``.

    The deterministic anti-hack teeth at plan time (§4 Layer 2): the plan must declare
    the SAME generic-shape decision the human ratified — a CORE-seam chokepoint (never an
    overlay-package patch), the constraint expressed as the ratified setting with its neutral
    default, the ratified per-overlay activation, the N=2-litmus rejected alternatives, and
    every refactor the sketch names. Any divergence is a finding the gate turns into a coder-
    dispatch block. This is only reached WITH a ratified sketch, so a section-level
    ``none_reason`` waiver is contradictory (a mechanism WAS ratified) and does NOT waive —
    the never-lockout escape is the audited all-reasoned-negative plan-bypass manifest.
    (Directive tickets are checked unconditionally per H3.)
    """
    if is_plan_bypass_shaped(adequacy):
        return None
    section = _mechanism_placement_declaration(adequacy)
    if not section:
        return (
            "the plan declares no mechanism_placement section — a directive ticket's plan must record the "
            "generic-shape decision (core-seam chokepoint, setting + neutral default, per-overlay activation, "
            "rejected alternatives) that conforms to the ratified sketch"
        )
    chokepoint = str(section.get("policy_chokepoint", "")).strip()
    if not is_core_seam_chokepoint(chokepoint):
        return (
            f"mechanism_placement declares chokepoint {chokepoint or '<none>'!r}, which is not a core seam — the "
            f"constraint must live at a src/teatree/... chokepoint every overlay flows through, never an "
            f"overlay-package one-off (the hack the N=2 litmus rejects)"
        )
    return _conformance_finding(section, sketch, chokepoint)


def _typed_equal(declared: object, ratified: object) -> bool:
    """Value equality that is TYPE-aware — ``0`` (int) never matches ``False`` (bool).

    Plain ``==`` collapses ``0 == False`` / ``1 == True``, so a JSON ``false`` plan value
    would silently pass as the int ``0`` ratified neutral default. Requiring identical
    types keeps a bool-vs-int (or int-vs-float) neutral_default/activation_value a drift.
    """
    return type(declared) is type(ratified) and declared == ratified


def _conformance_finding(section: "Mapping[str, object]", sketch: MechanismSketch, chokepoint: str) -> str | None:
    drifts: tuple[tuple[bool, str], ...] = (
        (chokepoint != sketch.policy_chokepoint, "chokepoint"),
        (str(section.get("setting_key", "")).strip() != sketch.setting_key, "setting_key"),
        (not _typed_equal(section.get("neutral_default"), sketch.neutral_default), "neutral_default"),
        (str(section.get("activation_scope", "")).strip() != sketch.activation_scope, "activation_scope"),
        (not _typed_equal(section.get("activation_value"), sketch.activation_value), "activation_value"),
    )
    for failed, field in drifts:
        if failed:
            return f"mechanism_placement {field} drifts from the ratified sketch"
    if not _declared_str_list(section.get("rejected_alternatives")):
        return (
            "mechanism_placement names no rejected_alternatives — the N=2 litmus (this generic shape over the "
            "overlay-local one-off) must be recorded"
        )
    declared_refactors = set(_declared_str_list(section.get("refactors")))
    missing = [refactor for refactor in sketch.refactors if refactor not in declared_refactors]
    if missing:
        return f"mechanism_placement omits refactors the ratified sketch requires: {', '.join(missing)}"
    return None
