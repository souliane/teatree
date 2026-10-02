"""The ONE factory-intake decision function — one trust boundary, evaluated top-down (#3634).

Every issue-intake path answers to this table, first match wins:

1. ``needs-triage`` present -> IGNORE (maintainer hold).
2. One of the overlay's ``exclude_labels`` present -> IGNORE (operator hold, #4134).
3. The row is an umbrella/epic tracking parent -> IGNORE (#4105).
4. An active ticket / claim / forge read-back already exists -> IGNORE (work exists).
5. Author trusted -> ACT immediately: no admit label, no assignment, no grace window.
6. Author untrusted AND the owner-applied admit label present -> ACT.
7. Author untrusted, no label -> IGNORE (fail-closed).

Rules 1 and 2 are the same kind of thing — a label the human applied to withhold an
issue — so they sit together, above every reason to act. They keep separate verdicts
because ``needs-triage`` is a shipped convention and the denylist is per-overlay data,
and the log line has to say which one held the issue. The denylist outranks the umbrella
rule too: an explicit per-issue operator act beats a structural classification, so a row
that is both an epic and excluded logs the exclusion its operator wrote.

The umbrella rule sits above "work exists" because umbrella-ness is a property of the ROW,
not of what has happened to it — an epic is not an implementable unit whatever else is
true, and the verdict that says so is the one an operator needs in the log.

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

from teatree.core.intake.label_admission import excluded
from teatree.core.intake.umbrella import normalize_labels, umbrella_reason
from teatree.core.models.implemented_issue_marker import NEEDS_TRIAGE_LABEL
from teatree.types import RawAPIDict

#: The shipped default admission label. The effective value is the
#: ``issue_implementer_label`` setting — see :func:`resolve_admit_label`.
DEFAULT_ADMIT_LABEL = "t3-auto"


class IntakeVerdict(StrEnum):
    """Which rule of the decision table matched, and hence what the factory does."""

    IGNORE_NEEDS_TRIAGE = "ignore_needs_triage"
    IGNORE_EXCLUDED_LABEL = "ignore_excluded_label"
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


@dataclass(frozen=True, slots=True)
class IntakeLabelPolicy:
    """The overlay's two configured label SETS — the operator half of the label rules.

    One value rather than two parallel arguments, because they are read off one overlay
    config together and a caller that passes one and forgets the other silently loses a
    whole rule. Distinct from :class:`~teatree.core.intake.label_admission.LabelPolicy`,
    which is the GitLab sync path's allowlist/denylist pair.
    """

    exclude: frozenset[str] = frozenset()
    umbrella: frozenset[str] = frozenset()


#: The "nothing configured" policy: excludes nobody, and leaves the umbrella LABEL signal
#: off while the structural one still decides. A caller that omits the policy gets this.
NO_LABEL_POLICY = IntakeLabelPolicy()


def decide_intake(
    facts: IntakeFacts,
    *,
    admit_label: str,
    exclude_labels: frozenset[str] = frozenset(),
) -> IntakeVerdict:
    """Apply the decision table to *facts*, top-down, first match wins.

    The table is a literal ordered sequence rather than a chain of early returns, so the
    ORDER — the thing every rule's precedence argument is about — is one readable list.
    Every predicate is a pure set/flag read, so evaluating them all costs nothing.

    ``exclude_labels`` is the overlay's denylist. It defaults to EMPTY, which excludes
    nothing — an overlay that never configured one keeps its pre-#4134 verdicts.
    """
    table = (
        (NEEDS_TRIAGE_LABEL in facts.labels, IntakeVerdict.IGNORE_NEEDS_TRIAGE),
        (excluded(facts.labels, exclude_labels), IntakeVerdict.IGNORE_EXCLUDED_LABEL),
        (bool(facts.umbrella_reason), IntakeVerdict.IGNORE_UMBRELLA),
        (facts.work_exists, IntakeVerdict.IGNORE_WORK_EXISTS),
        (facts.author_trusted, IntakeVerdict.ACT_TRUSTED_AUTHOR),
        (bool(admit_label) and admit_label in facts.labels, IntakeVerdict.ACT_ADMITTED),
    )
    for matched, verdict in table:
        if matched:
            return verdict
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
    label_policy: IntakeLabelPolicy = NO_LABEL_POLICY,
) -> IntakeVerdict:
    """:func:`decide_intake` against a raw forge issue payload.

    An empty ``label_policy.umbrella`` means no marker labels are configured, not "use
    the shipped set" — the structural signal still decides on the body alone.
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
                umbrella_labels=label_policy.umbrella,
            ),
        ),
        admit_label=admit_label,
        exclude_labels=label_policy.exclude,
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
