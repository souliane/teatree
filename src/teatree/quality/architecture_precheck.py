"""Deterministic validator for the architecture pre-check section (PR-11).

The architecture-design companion (``skills/architecture-design/SKILL.md``) asks
the implementer to fill a numbered pre-check before touching ``src/`` and to
carry its summary into the PR body's ``## Architecture pre-check`` section. This
module turns "every required check is answered" into a deterministic check, so a
missing answer is caught mechanically rather than only by a reviewer's eye.

PR-11 adds the tenth check — **removability / harness-vs-data**: a design must
state whether the proposed component is removable and whether it belongs in the
harness or in data/config. :func:`precheck_findings` fires a finding for any
required check whose section is absent or left as an unanswered ``<placeholder>``;
the removability finding is the one this PR introduces.
"""

import re
from dataclasses import dataclass

# A section STARTS at a numbered heading (``## 1. …``) and ENDS at the next
# heading of any level — so a trailing ``## Workflow`` never bleeds into a
# section's body and makes a bare-placeholder answer look filled in.
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_NUMBERED_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(\d+)\.\s")
# A body line carrying only the template's ``<angle-bracket>`` stub is a
# placeholder; a real citation or "n/a — …" is an answer.
_PLACEHOLDER_RE = re.compile(r"^<[^>]*>$")


@dataclass(frozen=True)
class PrecheckItem:
    """One required check in the numbered architecture pre-check template."""

    number: int
    key: str
    title: str


REQUIRED_CHECKS: tuple[PrecheckItem, ...] = (
    PrecheckItem(1, "blueprint_alignment", "BLUEPRINT § alignment"),
    PrecheckItem(2, "fsm_phase_boundaries", "FSM phase boundaries"),
    PrecheckItem(3, "extension_point_contracts", "Extension-point contracts"),
    PrecheckItem(4, "component_boundaries", "Component boundaries"),
    PrecheckItem(5, "dependency_direction", "Dependency direction"),
    PrecheckItem(6, "test_surface", "Test surface"),
    PrecheckItem(7, "resilience_invariants", "Resilience invariants"),
    PrecheckItem(8, "identity_normalization", "Identity and key normalization"),
    PrecheckItem(9, "behavior_preservation", "Behavior preservation / capability deletion"),
    PrecheckItem(10, "removability", "Removability / harness-vs-data"),
)

#: The check PR-11 adds — asked whether the component is removable and whether it
#: belongs in the harness or in data/config.
REMOVABILITY_CHECK: PrecheckItem = REQUIRED_CHECKS[-1]


def parse_sections(text: str) -> dict[int, str]:
    """Return ``{check_number: body}`` for every ``## N. <title>`` heading in *text*.

    Body is the stripped lines between a numbered heading and the next heading of
    any level. A duplicate number keeps the LAST occurrence — a re-filled section
    governs.
    """
    sections: dict[int, str] = {}
    current: int | None = None
    body: list[str] = []
    for line in text.splitlines():
        if _ANY_HEADING_RE.match(line):
            if current is not None:
                sections[current] = "\n".join(body).strip()
                current = None
                body = []
            numbered = _NUMBERED_HEADING_RE.match(line)
            if numbered:
                current = int(numbered.group(1))
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body).strip()
    return sections


def is_answered(body: str) -> bool:
    """True when *body* carries a real answer, not an empty / placeholder stub.

    A body is unanswered iff every non-empty line is a bare ``<placeholder>``
    (or there are none). Any line of real content — a citation, ``n/a — …`` —
    makes it answered.
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return any(not _PLACEHOLDER_RE.match(line) for line in lines)


def precheck_findings(text: str) -> list[str]:
    """Return a finding per required check that is absent or left unanswered.

    Vacuous-on-empty: an empty / whitespace-only *text* returns ``[]`` — there is
    no pre-check to validate. A non-empty pre-check is validated in full; the
    removability finding (PR-11's new check) fires when check 10 is missing or a
    bare placeholder.
    """
    if not text.strip():
        return []
    sections = parse_sections(text)
    findings: list[str] = []
    for check in REQUIRED_CHECKS:
        body = sections.get(check.number)
        if body is None or not is_answered(body):
            findings.append(f"check {check.number} ({check.title}) is unanswered")
    return findings


def removability_answered(text: str) -> bool:
    """True iff the removability / harness-vs-data check (PR-11) is answered."""
    body = parse_sections(text).get(REMOVABILITY_CHECK.number)
    return body is not None and is_answered(body)
