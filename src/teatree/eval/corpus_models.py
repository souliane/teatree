"""Typed ground-truth label for one captured-session corpus entry.

The corpus closes the circular-oracle gap in the matcher suite: a
:mod:`teatree.eval.scenarios` spec is written by the same author as the rule it
pins, so the spec's assertions and the rule cannot disagree. A corpus entry
instead grades a CAPTURED real session (``<entry_id>.session.jsonl``) against an
EXTERNALLY-authored label (``<entry_id>.label.yaml``) — the
:attr:`CorpusLabel.labelled_by` who decided the expected behaviour is recorded
separately from the :attr:`CorpusLabel.rule_author` who wrote the rule, so the
anti-circular guard (:func:`teatree.eval.corpus_grade.assert_independent_oracle`)
can refuse a matcher-graded entry whose label and rule share one author.

``outcome_axis`` / ``expected_outcome`` are the categorical ground-truth: the
axis names a dimension a session is judged on (e.g. ``"backgrounding"``) and the
expected_outcome is its labelled value (e.g. ``"backgrounded"``). They are the
confusion-matrix substrate the audit layer (:class:`SessionAuditRecord`)
produces ``(expected, predicted)`` pairs into.
"""

import dataclasses
from typing import Literal

from teatree.eval.models import ExpectItem, JudgeSpec

Confidence = Literal["high", "medium", "low"]
Oracle = Literal["matcher", "judge", "both"]


@dataclasses.dataclass(frozen=True)
class CorpusLabel:
    """An externally-authored ground-truth label for one captured session."""

    entry_id: str
    labelled_by: str
    labelled_at: str
    expected_behavior: str
    outcome_axis: str
    expected_outcome: str
    confidence: Confidence
    oracle: Oracle
    matchers: tuple[ExpectItem, ...]
    judge: JudgeSpec | None
    rule_author: str = ""
    source_session_id: str = ""
