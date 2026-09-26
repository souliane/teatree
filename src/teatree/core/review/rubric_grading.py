"""Validate a reviewer-shaped rubric-grades payload — the ONE coverage/contradiction check.

Both the reviewing-phase envelope recorder (:mod:`teatree.agents.review_envelope_recorder`)
and the operator-facing ``review record --rubric-grades-json`` CLI run this BEFORE writing
anything, so an agent's returned verdict and a human operator's recording can never disagree
about what a rubric grading requires (souliane/teatree#4832). Lives here rather than in
:mod:`teatree.agents.review_envelope_recorder` because the CLI command
(``core.management.commands``) may not depend on ``teatree.agents`` (the dependency graph
runs the other way); a plain leaf under ``core.review`` is reachable from both.

Deliberately pure — it stamps nothing. The caller applies the returned grades via
:meth:`~teatree.core.models.rubric.Rubric.apply_grades` inside its OWN transaction, alongside
whatever else (a ``ReviewVerdict`` row) must land atomically with them.
"""

from teatree.core.models.rubric import Rubric, RubricCriterion, RubricError
from teatree.core.models.types import RubricGrade

#: Mirrors ``ReviewVerdict.Verdict.MERGE_SAFE`` without importing ``review_verdict`` — this
#: leaf has no need of the merge-precondition machinery that module also carries.
_MERGE_SAFE = "merge_safe"


def validate_rubric_grades(
    rubric: "Rubric | None",
    grades_payload: object,
    *,
    verdict: str,
) -> tuple[str, list[RubricGrade]]:
    """Validate *grades_payload* against *rubric*'s coverage, or return the refusal reason.

    Returns ``(refusal, grades)``: ``("", [])`` when *rubric* is ``None`` (nothing owed — a
    ticket with no rubric, or a PR no ticket owns, owes no grades); ``("", normalized_grades)``
    when every criterion is covered and no contradiction was found; ``(reason, [])`` on a
    refusal — stamp NOTHING when this returns a reason.

    Refuses when: a criterion is left ungraded (recording a verdict over an ungraded rubric
    would retire the review claim while the done-gate still refuses the merge, leaving the
    head unmergeable with no reviewer left to re-arm — true of ANY recorded verdict, not just
    ``merge_safe``); *verdict* is ``merge_safe`` but a graded criterion is FAIL (a verdict
    cannot both vouch for the head and record an unmet acceptance criterion); or the payload
    itself is malformed (:meth:`Rubric.normalize_grades`'s own refusal).
    """
    if rubric is None:
        return "", []
    try:
        grades: list[RubricGrade] = Rubric.normalize_grades(grades_payload)
    except RubricError as exc:
        return str(exc), []
    ungraded = rubric.ungraded_ordinals(grades)
    if ungraded:
        named = ", ".join(f"#{ordinal}" for ordinal in ungraded)
        message = (
            f"criteria {named} of ticket {rubric.ticket.pk} are ungraded "
            f"({_returned_grades_phrase(grades_payload)}) — recording this verdict would retire the review "
            "claim while the done-gate still refuses the merge, leaving the head unmergeable with no "
            "reviewer left to re-arm. Grade EVERY criterion and record again"
        )
        return message, []
    if str(verdict).strip().lower() == _MERGE_SAFE:
        failed = [
            grade["ordinal"] for grade in grades if grade["status"].strip().lower() == RubricCriterion.Status.FAIL
        ]
        if failed:
            named = ", ".join(f"#{ordinal}" for ordinal in failed)
            message = (
                f"criteria {named} are graded FAIL but the verdict is merge_safe — a verdict cannot both "
                "vouch for the head and record an unmet acceptance criterion"
            )
            return message, []
    return "", grades


def _returned_grades_phrase(returned: object) -> str:
    """What the caller actually carried under ``rubric_grades`` — the actionable half.

    Naming only the criteria leaves a caller that passed ``{"0": "pass"}`` reading a
    refusal about coverage when its payload was never a list of grades at all.
    """
    if returned is None:
        return 'you returned no "rubric_grades" at all'
    if isinstance(returned, list):
        return f'your "rubric_grades" carried {len(returned)} grade(s)'
    return f'your "rubric_grades" was a {type(returned).__name__}, not a JSON array'


__all__ = ["validate_rubric_grades"]
