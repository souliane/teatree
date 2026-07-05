"""Pure plan-adequacy validators (SELFCATCH-3) — the structural anti-thin-spec check.

The named root cause of the 26-bug integration campaign was thin-spec-as-plan: a
``PlanArtifact`` satisfied the plan gate on ``plan_text.strip()`` alone, so a
scope+acceptance spec passed as a plan and a coder was dispatched with nothing
naming the seams the change touched. These pure functions are the structural
substitute — the four-section manifest each real plan must carry.

A leaf module (no DB, no gate imports) so both the model (``plan_artifact``,
which enforces the manifest at ``record`` time under the flag) and the gate
(``plan_currency_gate``, which re-checks it and reads the declared seams) import
DOWN to it — never a model→gate up-edge. Mirrors ``anti_vacuity_gate.is_complete``:
each section is substantive OR an explicit reasoned negative, and silence never
passes.
"""

import string
from typing import TYPE_CHECKING, cast

from teatree.core.models.types import AdequacySection, PlanAdequacy

if TYPE_CHECKING:
    from collections.abc import Mapping

# The four sections every real plan must speak to. Missing any one is a thin spec.
REQUIRED_ADEQUACY_SECTIONS: tuple[str, ...] = ("design", "integration_seams", "edge_cases", "test_strategy")

_SHA_LEN = 40


def is_valid_base_sha(base_sha: object) -> bool:
    """Whether *base_sha* is a full 40-char hex commit SHA (the plan's authored base)."""
    if not isinstance(base_sha, str):
        return False
    cleaned = base_sha.strip()
    return len(cleaned) == _SHA_LEN and all(c in string.hexdigits for c in cleaned)


def section_complete(section: object) -> bool:
    """Whether one adequacy *section* is substantive OR carries an explicit reasoned negative.

    Substantive = non-empty text, or a list with at least one non-blank item.
    Otherwise a non-blank ``none_reason`` (the explicit reasoned negative) satisfies
    it. Both empty — the forgotten/silent section — never passes.
    """
    if not isinstance(section, dict):
        return False
    fields = cast("Mapping[str, object]", section)
    content = fields.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list) and any(str(item).strip() for item in content):
        return True
    none_reason = fields.get("none_reason")
    return isinstance(none_reason, str) and bool(none_reason.strip())


def is_adequate(adequacy: object) -> bool:
    """Whether *adequacy* is a complete four-section manifest.

    Every required section must be present and complete (:func:`section_complete`).
    A scope+acceptance-only thin spec — no seams/edge-cases/test-strategy claims —
    fails here, which is the whole point.
    """
    if not isinstance(adequacy, dict):
        return False
    sections = cast("Mapping[str, object]", adequacy)
    return all(section_complete(sections.get(name)) for name in REQUIRED_ADEQUACY_SECTIONS)


def declared_seam_paths(adequacy: object) -> tuple[str, ...]:
    """The registries/contracts/paths the plan declares it touches (its integration seams).

    Read from the ``integration_seams`` section's ``content``. An explicit
    ``no_seams`` negative (``none_reason`` set, no content) yields ``()`` — the
    plan claims no seams, so the currency gate finds nothing to guard (a diff that
    later touches a known seam is the DEFERRED post-diff seam-parity checker's job,
    not this one's). Empty on a missing/malformed manifest.
    """
    if not isinstance(adequacy, dict):
        return ()
    section = cast("Mapping[str, object]", adequacy).get("integration_seams")
    if not isinstance(section, dict):
        return ()
    content = cast("Mapping[str, object]", section).get("content")
    if isinstance(content, list):
        return tuple(str(path).strip() for path in content if str(path).strip())
    if isinstance(content, str) and content.strip():
        return (content.strip(),)
    return ()


def negated_section(reason: str) -> AdequacySection:
    """An explicit reasoned-negative section — the ``no_seams: {reason}`` shape."""
    return AdequacySection(none_reason=reason)


def all_negated_adequacy(reason: str) -> PlanAdequacy:
    """A minimal-but-VALID manifest, every section an explicit reasoned negative.

    The shape an audited bypass / plan-reaffirm with genuinely nothing to declare
    records: it passes :func:`is_adequate` (no section is silent) while asserting
    the change touches no seams, has no edge cases, and adds no tests — all with
    the same stated *reason*.
    """
    return PlanAdequacy(
        design=AdequacySection(content=reason),
        integration_seams=negated_section(reason),
        edge_cases=negated_section(reason),
        test_strategy=negated_section(reason),
    )
