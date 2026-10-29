"""Validate and apply review rubric grades at a recorded head."""

import dataclasses
from typing import TYPE_CHECKING

from teatree.agents.envelope_refusal import MALFORMED_RUBRIC_GRADES_PREFIX
from teatree.core.models import ReviewVerdict, Rubric, RubricCriterion

if TYPE_CHECKING:
    from teatree.agents.result_schema import ReviewVerdictEnvelope
    from teatree.core.models.types import RubricGrade


@dataclasses.dataclass(frozen=True)
class ReviewGrading:
    rubric: Rubric | None
    grades: "list[RubricGrade]"
    carried: frozenset[int]
    prior_head: str

    def apply(self, *, identity: str, head: str) -> None:
        if self.rubric is None:
            return
        self.rubric.apply_grades(self.grades, grader_identity=identity, reviewed_sha=head)
        if self.carried and self.prior_head:
            pks = {str(pk) for pk in self.rubric.criteria.filter(ordinal__in=self.carried).values_list("pk", flat=True)}
            Rubric.carry_grades(self.rubric.ticket, from_head=self.prior_head, to_head=head, only=pks)


def _returned_grades_phrase(returned: object) -> str:
    if returned is None:
        return 'you returned no "rubric_grades" at all'
    if isinstance(returned, list):
        return f'your "rubric_grades" carried {len(returned)} grade(s)'
    return f'your "rubric_grades" was a {type(returned).__name__}, not a JSON array'


def rubric_coverage_error(
    rubric: "Rubric | None",
    grades: "list[RubricGrade]",
    *,
    returned: object,
    carried: frozenset[int] = frozenset(),
) -> str:
    """Refuse a verdict that leaves any criterion of *rubric* ungraded, or ``""``.

    ORDERING IS LOAD-BEARING, which is why this runs BEFORE any write.
    ``ReviewVerdict.record`` retires the per-head claim and releases the review lock in
    the same transaction that records the verdict — so a verdict written over a
    half-graded rubric leaves nothing to re-arm review, while the done-gate goes on
    refusing the merge for the PENDING criterion. The head is then unmergeable forever.

    A ticket with no rubric, and a PR no ticket owns, owe no grades at all: that is
    byte-for-byte the subject ``core.merge.ticket_gates`` already skips, and widening it
    here would refuse a colleague's PR the merge gate never grades.
    """
    if rubric is None:
        return ""
    ungraded = [ordinal for ordinal in rubric.ungraded_ordinals(grades) if ordinal not in carried]
    if not ungraded:
        return ""
    named = ", ".join(f"#{ordinal}" for ordinal in ungraded)
    return (
        f"{MALFORMED_RUBRIC_GRADES_PREFIX}criteria {named} of ticket {rubric.ticket.pk} are ungraded "
        f"({_returned_grades_phrase(returned)}) — "
        f"a verdict that leaves a criterion PENDING records nothing, because recording it would retire "
        f"this head's review claim while the done-gate still refuses the merge, leaving the head "
        f"unmergeable with no reviewer left to re-arm. You are the independent verifier: grade EVERY "
        f"criterion and return the verdict again, as "
        '"rubric_grades": [{"ordinal": <int>, "text_hash": "<hash from brief>", '
        '"status": "pass"|"fail", "rationale": "<what proves it>"}] '
        f"— a PASS cites the test that proves it, a FAIL needs no citation"
    )


def merge_safe_over_fail_error(
    rubric: "Rubric | None", envelope: "ReviewVerdictEnvelope", grades: "list[RubricGrade]"
) -> str:
    """Refuse a ``merge_safe`` verdict carrying a criterion the same envelope grades FAIL.

    The sibling of ``ReviewVerdict._assert_checks_admit_merge_safe`` one field over: that
    one refuses merge_safe over checks the reviewer itself reported RED, this one over an
    acceptance criterion the reviewer itself grades unmet. Both are a contradiction between
    two fields ONE reviewer wrote in ONE envelope, and neither is a statement about the tree.

    Recorded, it retires the per-head claim and releases the lock, and the done-gate then
    refuses the merge on a FAIL no bypass overrides — recoverable only by a push that mints
    a new head. Refusing before the write keeps the head re-reviewable instead.

    Scoped to a rubric that EXISTS, like the coverage refusal beside it: with no rubric the
    grade is stamped nowhere, so there is no FAIL for the done-gate to refuse the merge on.
    """
    if rubric is None or str(envelope.get("verdict", "")).strip().lower() != ReviewVerdict.Verdict.MERGE_SAFE:
        return ""
    failed = [grade["ordinal"] for grade in grades if grade["status"].strip().lower() == RubricCriterion.Status.FAIL]
    if not failed:
        return ""
    named = ", ".join(f"#{ordinal}" for ordinal in failed)
    return (
        f"{MALFORMED_RUBRIC_GRADES_PREFIX}your verdict is merge_safe but you graded criteria {named} FAIL — "
        f"one envelope cannot both vouch for the head and record an unmet acceptance criterion, and a FAIL is "
        f"never overridable, so recording this would retire the review claim onto a merge the done-gate refuses "
        f"forever. Return `hold` if the criteria really are unmet, or re-grade them honestly if they are not"
    )
