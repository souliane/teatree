"""The reviewer-brief doctrine both ``core.models`` and ``agents`` render — a pure leaf.

``modelkit`` (``depends_on = []``) is the only stratum
:mod:`teatree.core.models.auto_review_dispatch` and :mod:`teatree.agents.phase_blocks`
can both import, so the sentence they each stamp into a reviewer's brief lives here
once instead of being reworded independently in each.

The rule below exists because the brief and the review skill had drifted into saying
opposite things about the same array, and the brief wins on ordering: it is stamped
into ``Task.execution_reason`` and read before any skill loads. ``/t3:review``
suppresses a clean-check finding on the colleague-facing lane, where noise under the
owner's identity costs credibility; the ``review_verdict`` envelope is a durable
internal record nothing publishes, so the same suppression there buys nothing and
costs recall. 81.4% of passing verdicts (416/511) arrived with an empty ``findings``
array against a median merged diff of 287 lines.
"""

from typing import Final

CRITIC_REMEDY: Final[str] = (
    "Inspect the failed critic tasks; record a verified verdict with `t3 <overlay> ticket critic-record` "
    "or push a new commit."
)

#: Stamped verbatim into every reviewer-facing brief teatree renders, and mirrored
#: in the ``findings`` JSON-schema description a structured-output model reads.
ENVELOPE_FINDINGS_RULE: Final[str] = (
    'Record all observations in "findings": low severity, uncertainty, and unreadable checks too. '
    'This is internal, never posted. A merge_safe with empty findings means none found. Name "basis" '
    '(executed, documentation, venue_limit); executed failures give command and result in "reproduction". '
    "Untested behaviour and duplicate definitions may cite file:line. A hold records blocking findings; "
    "unexecuted documentation and venue limits are notes."
)

#: Stamped beside :data:`ENVELOPE_FINDINGS_RULE` into every reviewer-facing brief that
#: returns a verdict envelope. ``ReviewVerdict.record`` refuses a ``merge_safe`` verdict
#: whose ``gh_verify_result`` is ``failed`` (§17.8 clause 3) and that refusal is correct,
#: but it is unsatisfiable by re-reviewing the SAME head — the checks stay red — so the
#: contradiction has to be prevented where the reviewer writes it rather than caught after.
#: Prompt-side and unfalsifiable on its own; the enforcing halves are the refusal itself and
#: :meth:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch.mark_refused`.
VERDICT_CHECKS_RULE: Final[str] = (
    'A RED required check is a "hold", never a "merge_safe". When the pull request\'s required checks are '
    'failing, that is a complete and recordable review: set "verdict" to "hold", report '
    '"gh_verify_result" as "failed", and name the failing check in "findings". A "merge_safe" carrying '
    '"gh_verify_result": "failed" contradicts itself and is REFUSED — nothing is recorded, your whole run '
    "is discarded, and a live CI read at the reviewed SHA decides whether this head then stops for a human. "
    "Report the checks as you found "
    "them; the verdict follows from them, never the other way round."
)
