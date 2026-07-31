"""An acceptance criterion with no failing state of the world is not a criterion (#3762).

The repo already requires every regression test be proven RED before its fix — a
test that passes on the buggy code guards nothing. That rule was never extended
from *tests* to *feature-level acceptance criteria*, and souliane/teatree PR
This is what the gap costs: the skipped phase's acceptance criterion was "the
existing resolver test suite passes UNMODIFIED", and the PR satisfied it by
stating "resolution.py was not touched". A resolver suite passes unmodified
precisely when the resolver is never modified, so the criterion was satisfiable
by INACTION and certified the skip as a success.

"X stays unchanged" is the highest-risk criterion shape — it is satisfied by
absence. It is not banned outright (a genuine no-regression claim is worth
stating); it must be PAIRED with at least one positive criterion that only the
implemented feature can satisfy. This module is the pure detector; the
enforcement seam is :meth:`teatree.core.models.rubric.Rubric.populate`, so a
rubric can never be populated with an all-absence checklist that would then be
graded PASS by a verifier who reads it literally and correctly.

Detection is lexical and deliberately conservative: it fires on a criterion whose
ONLY assertion is about something not happening. A criterion that names a
positive observable and merely mentions an unchanged input is not flagged.
"""

import re
from typing import Final

#: Phrases whose whole content is "nothing happened to X". Matched
#: case-insensitively against the criterion text; word boundaries keep
#: "unchanged" from matching inside a longer token.
_ABSENCE_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:stays?|remains?|is|are|was|were)\s+(?:the\s+same|unchanged|untouched|unmodified)\b",
    r"\bpass(?:es|ed|ing)?\s+(?:\w+\s+){0,3}?unmodified\b",
    r"\bwithout\s+(?:any\s+)?(?:modification|modifications|changes?)\b",
    r"\bno\s+(?:new\s+|other\s+)?(?:changes?|modifications?|regressions?|edits?)\b",
    r"\bis\s+not\s+(?:modified|changed|touched|altered)\b",
    r"\b(?:are|were|was)\s+not\s+(?:modified|changed|touched|altered)\b",
    r"\bnothing\s+(?:\w+\s+){0,3}?(?:is|was|are|were)\s+(?:touched|changed|modified)\b",
    r"\bdoes\s+not\s+change\b",
    r"\bdo\s+not\s+change\b",
    r"\bremain(?:s)?\s+intact\b",
)

_ABSENCE_RE: Final[re.Pattern[str]] = re.compile("|".join(_ABSENCE_PATTERNS), re.IGNORECASE)


def is_absence_satisfied(criterion: str) -> bool:
    """True iff *criterion* is satisfied by INACTION — nothing makes it FAIL.

    Fires on the "X stays unchanged" family: a criterion asserting only that
    something did not change, was not touched, or still passes unmodified. A
    criterion that also names a positive observable is not flagged — the
    detector looks for the absence claim as the criterion's assertion, not as an
    incidental description of an input.
    """
    text = criterion.strip()
    if not text:
        return False
    return bool(_ABSENCE_RE.search(text)) and not _names_a_positive_observable(text)


#: Verbs that assert something the implemented feature must actively DO. Their
#: presence means the criterion has a failing state of the world even when it
#: also mentions an unchanged input.
_POSITIVE_VERB_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:returns?|reads?|writes?|emits?|raises?|refuses?|renders?|records?|"
    r"resolves?|produces?|exits?|blocks?|reports?|shows?|logs?|creates?)\b",
    re.IGNORECASE,
)


def _names_a_positive_observable(text: str) -> bool:
    """True iff the criterion also asserts an active, observable behaviour.

    The disambiguator for "re-running the resolver against an unchanged config
    returns the cached value": the absence phrase describes the INPUT, while the
    criterion's actual assertion ("returns the cached value") still fails when
    the feature is absent.
    """
    return bool(_POSITIVE_VERB_RE.search(text))


def absence_satisfied_criteria(criteria: list[str]) -> list[tuple[int, str]]:
    """The ``(1-based ordinal, text)`` of every criterion satisfiable by inaction."""
    return [(index, text) for index, text in enumerate(criteria, start=1) if is_absence_satisfied(text)]


def falsifiability_violation(criteria: list[str]) -> str:
    """The refusal message for an unfalsifiable checklist, or ``""`` when it is sound.

    A checklist is unsound when it contains an absence-satisfied criterion and
    NO positive one to pair it with — every state of the world satisfies it, so
    grading it PASS proves nothing about the feature. An empty list is not this
    module's concern (``Rubric.populate`` refuses it as the sibling vacuity).
    """
    offenders = absence_satisfied_criteria(criteria)
    if not offenders or len(offenders) < len(criteria):
        return ""
    listed = "\n".join(f"  {ordinal}. {text}" for ordinal, text in offenders)
    return (
        f"every acceptance criterion is satisfiable by INACTION — there is no state of the world that "
        f"makes it FAIL, so grading it PASS certifies nothing:\n{listed}\n"
        f"A criterion of the 'X stays unchanged' shape is satisfied by absence: it is met precisely when "
        f"the work is skipped, which is how a silently-skipped implementation phase gets certified as a "
        f"success (#3762). Pair it with at least one POSITIVE criterion only the implemented feature can "
        f"satisfy — name the observable the feature produces (what is read, returned, refused, rendered) "
        f"and the state of the world in which that observation fails."
    )
