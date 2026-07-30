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

#: Stamped verbatim into every reviewer-facing brief teatree renders, and mirrored
#: in the ``findings`` JSON-schema description a structured-output model reads.
ENVELOPE_FINDINGS_RULE: Final[str] = (
    'Record in "findings" what you actually observed — the low-severity and the uncertain ones too, '
    "and anything you could not check — whatever verdict you reach. Nothing here is published, so an "
    'omitted observation is a lost record rather than spared noise: "findings" is what you looked at, '
    '"verdict" is the separate judgement. A "hold" carries the observations that block. A "merge_safe" '
    'with an empty "findings" asserts you looked and found nothing worth recording, so emit that only '
    "when it is true."
)
