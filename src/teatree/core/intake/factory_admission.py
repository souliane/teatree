"""The ONE factory-intake decision function — one trust boundary, evaluated top-down (#3634).

Every issue-intake path answers to this table, first match wins:

1. ``needs-triage`` present -> IGNORE (maintainer hold).
2. The row is an umbrella/epic tracking parent -> IGNORE (#4105).
3. An active ticket / claim / forge read-back already exists -> IGNORE (work exists).
4. Author trusted -> ACT immediately: no admit label, no assignment, no grace window.
5. Author untrusted AND the owner-applied admit label present -> ACT.
6. Author untrusted, no label -> IGNORE (fail-closed).

Rule 2 sits above "work exists" because umbrella-ness is a property of the ROW, not of
what has happened to it — an epic is not an implementable unit whatever else is true, and
the verdict that says so is the one an operator needs in the log.

The two facts the caller must supply are the ones this module cannot compute
cheaply: *author_trusted* (the fail-closed
:mod:`~teatree.core.review.author_trust` gate) and *work_exists* (the ticket /
marker / read-back probe). Everything else is read off the payload here, so a
caller cannot hold a divergent opinion about labels.

Fail-closed in both directions: an unset admit label admits NOBODY (the admit-label
rule can never degrade to "any label admits"), and an author the caller could not
resolve arrives as ``author_trusted=False``.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from teatree.core.intake.umbrella import normalize_labels, umbrella_reason
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.types import RawAPIDict

#: The shipped default admission label. The effective value is the
#: ``issue_implementer_label`` setting — see :func:`resolve_admit_label`.
DEFAULT_ADMIT_LABEL = "t3-auto"


class IntakeVerdict(StrEnum):
    """Which rule of the decision table matched, and hence what the factory does."""

    IGNORE_NEEDS_TRIAGE = "ignore_needs_triage"
    IGNORE_UMBRELLA = "ignore_umbrella"
    IGNORE_WORK_EXISTS = "ignore_work_exists"
    ACT_TRUSTED_AUTHOR = "act_trusted_author"
    ACT_ADMITTED = "act_admitted"
    IGNORE_NOT_ADMITTED = "ignore_not_admitted"

    @property
    def acts(self) -> bool:
        return self in {IntakeVerdict.ACT_TRUSTED_AUTHOR, IntakeVerdict.ACT_ADMITTED}


@dataclass(frozen=True, slots=True)
class IntakeFacts:
    labels: frozenset[str]
    work_exists: bool
    author_trusted: bool
    #: Why the row reads as an umbrella parent, or ``""`` when it does not — the reason
    #: travels with the fact so the decline can say which signal fired (:mod:`.umbrella`).
    umbrella_reason: str = ""


def decide_intake(facts: IntakeFacts, *, admit_label: str) -> IntakeVerdict:
    """Apply the decision table to *facts*, top-down, first match wins."""
    if NEEDS_TRIAGE_LABEL in facts.labels:
        return IntakeVerdict.IGNORE_NEEDS_TRIAGE
    if facts.umbrella_reason:
        return IntakeVerdict.IGNORE_UMBRELLA
    if facts.work_exists:
        return IntakeVerdict.IGNORE_WORK_EXISTS
    if facts.author_trusted:
        return IntakeVerdict.ACT_TRUSTED_AUTHOR
    if admit_label and admit_label in facts.labels:
        return IntakeVerdict.ACT_ADMITTED
    return IntakeVerdict.IGNORE_NOT_ADMITTED


def payload_labels(payload: RawAPIDict) -> frozenset[str]:
    """Label names off a forge payload, across the string and ``{"name": ...}`` shapes."""
    raw = payload.get("labels")
    if not isinstance(raw, list):
        return frozenset()
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                names.add(name)
    return frozenset(names)


def payload_body(payload: RawAPIDict) -> str:
    """The issue body off a forge payload — GitHub's ``body``, GitLab's ``description``."""
    for name in ("body", "description"):
        value = payload.get(name)
        if isinstance(value, str):
            return value
    return ""


def decide_issue_intake(
    issue: RawAPIDict,
    *,
    author_trusted: bool,
    work_exists: bool,
    admit_label: str,
    umbrella_labels: frozenset[str] = frozenset(),
) -> IntakeVerdict:
    """:func:`decide_intake` against a raw forge issue payload.

    An empty *umbrella_labels* means no marker labels are configured, not "use the
    shipped set" — the structural signal still decides on the body alone.
    """
    labels = payload_labels(issue)
    return decide_intake(
        IntakeFacts(
            labels=labels,
            work_exists=work_exists,
            author_trusted=author_trusted,
            umbrella_reason=umbrella_reason(
                body=payload_body(issue),
                labels=labels,
                umbrella_labels=umbrella_labels,
            ),
        ),
        admit_label=admit_label,
    )


def resolve_admit_label(overlay: str) -> str:
    """The effective admit label for *overlay* — the ``issue_implementer_label`` setting.

    Falls back to :data:`DEFAULT_ADMIT_LABEL` so a deployment that never set the
    row still recognises the shipped convention.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return get_effective_settings(overlay or None).issue_implementer_label or DEFAULT_ADMIT_LABEL


def resolve_umbrella_labels(overlay: str) -> frozenset[str]:
    """The effective umbrella-marker labels for *overlay* — ``umbrella_issue_labels``.

    An explicitly emptied list resolves to the empty set rather than the shipped default:
    the operator turned the label signal off, and the structural signal still stands.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return normalize_labels(get_effective_settings(overlay or None).umbrella_issue_labels)
