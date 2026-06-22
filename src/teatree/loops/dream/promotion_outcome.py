"""The outcome value object returned by every dreaming eval-promotion attempt (#1933, #2346).

Lives in its own module so both the promotion writer (:mod:`teatree.loops.dream.promote`)
and the live-model gate (:mod:`teatree.loops.dream.live_gate`) can depend on it without a
circular import between them.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """The result of attempting to promote one candidate.

    ``promoted`` is the truth of the operation; ``reason`` always explains the
    decision (the rejecting guard message on a reject, the written paths on a
    promote). ``scenario_path``/``fail_fixture``/``pass_fixture`` are populated
    only on a successful promote.
    """

    scenario_name: str
    promoted: bool
    reason: str
    scenario_path: Path | None = None
    fail_fixture: Path | None = None
    pass_fixture: Path | None = None
    #: A non-promoted outcome that should be RETRIED on a later pass rather than
    #: recorded terminal-``rejected``. Set when the metered live check was simply
    #: NOT RUN (no validator, e.g. nightly tick): the candidate cleared scrub +
    #: anti-vacuity and only lacks a live verdict, so a later validated run can
    #: still land it. A live-FAIL (the grader does not fit the model) is NOT
    #: retryable — it is a verdict, recorded ``rejected``.
    retryable: bool = False
