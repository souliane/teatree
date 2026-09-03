"""Which checks snapshot admits a ``merge_safe`` verdict — §17.8 clause 3 (#4522, #4530, #4554).

The invariant is absolute and never weakens: a ``merge_safe`` row may not carry
``gh_verify_result=failed``, whatever CI says. What a live read decides is the
CLASSIFICATION of that refusal, which is what drives the terminal latch and the owner page.

``gh_verify_result`` is SELF-ASSERTED — the string a reviewer put in its own envelope — so a
refusal decided on it alone describes that reviewer, not the tree. Measured on this deploy,
6 of the 9 heads that ever raised it recorded a verdict at that SAME head afterwards, and 3
of those were a ``hold`` over checks that genuinely were red. So the read is consulted before
the refusal is called a contradiction, and only a red the forge itself confirms is terminal.

The read is LAZY and INJECTED: it fires only on the FAILED branch of a ``merge_safe``
verdict, before the recording transaction opens, and no model owns the network call. A
caller that supplies no probe reads UNREADABLE — refusing exactly as before, minus the
terminal claim it could not evidence.
"""

from typing import TYPE_CHECKING, NoReturn

from teatree.core.modelkit.forge_readability import LiveChecksProbe, LiveChecksRead
from teatree.core.models.merge_clear import MergeClear

if TYPE_CHECKING:
    from collections.abc import Callable


class ReviewVerdictError(ValueError):
    """A ``ReviewVerdict`` was rejected at record time — the contract failed."""


class ChecksContradictionError(ReviewVerdictError):
    """A ``merge_safe`` verdict over checks a LIVE read confirms are red (#4522, #4530, #4554).

    Raised only when the workflow-run read at the reviewed SHA corroborates the envelope's
    own ``failed`` report. A live GREEN or PENDING read makes the refusal an ordinary
    :class:`ReviewVerdictError` (the envelope misreported itself — a defect in one run), and
    a read that could not establish the state stays ordinary too, because latching on an
    unverified report is exactly the false refusal this class was measured to produce.

    A red head does not make a verdict impossible; it makes ``merge_safe`` impossible — the
    recordable outcome :data:`~teatree.core.modelkit.review_contract.VERDICT_CHECKS_RULE`
    asks reviewers for is a ``hold``, and that path never reaches here.

    The type, not the message text, is what the claim's TERMINAL
    (:meth:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch.mark_refused`) reads
    to tell an exhausted budget spent on a red tree from one spent on crashed reviewers.
    Every attempt before the bound is left alone.
    """


_INVARIANT: str = "a merge_safe verdict can never carry gh_verify_result=failed"


def live_checks_reader(probe: "LiveChecksProbe | None", *, slug: str, head_sha: str) -> "Callable[[], LiveChecksRead]":
    """Bind *probe* to the reviewed tree, deferred so the happy path never reaches the forge."""
    if probe is None:
        return lambda: LiveChecksRead.unreadable("no live-CI probe was supplied to this recording")
    return lambda: probe(slug=slug, head_sha=head_sha)


def assert_checks_admit_merge_safe(
    normalized_verify: str, *, expedited: bool, read_live: "Callable[[], LiveChecksRead]"
) -> None:
    """Refuse a ``merge_safe`` verdict whose checks snapshot cannot admit it.

    A FAILED snapshot is refused however the live read comes back — expedite can never waive
    a red required check — and the read only decides which refusal it is. PENDING is
    untouched by the read: a queued check is not a red one, and both the expedite waiver and
    the checks simply finishing make that same head recordable.
    """
    if normalized_verify == MergeClear.VerifyResult.FAILED:
        _refuse_failed_snapshot(read_live())
    if normalized_verify == MergeClear.VerifyResult.PENDING and not expedited:
        msg = (
            f"a merge_safe verdict on PENDING checks (got {normalized_verify!r}) requires the "
            f"expedite waiver (expedited=True) — a recorded HOLD on queued checks can never be "
            f"promoted to merge-safe by a later live re-check unless the CLEAR carries a "
            f"human-authorized, SHA-bound pending-waiver (§17.8 clause 3)"
        )
        raise ReviewVerdictError(msg)


def _refuse_failed_snapshot(live: LiveChecksRead) -> NoReturn:
    """Refuse the FAILED snapshot, typed by what the live read at the reviewed SHA established."""
    if live.is_failed:
        msg = (
            f"{_INVARIANT} — and a live workflow-run read at the reviewed SHA CONFIRMS red "
            f"({live.detail}). A FAILED required check is a real red verdict and expedite can "
            f"never waive it (§17.8 clause 3; mirrors MergeClear.issue refusing a failed CLEAR)"
        )
        raise ChecksContradictionError(msg)
    if live.is_unreadable:
        msg = (
            f"{_INVARIANT} — and the live workflow-run read could not establish the CI state at "
            f"the reviewed SHA ({live.detail}), so this report is UNVERIFIED rather than "
            f"confirmed. Nothing is recorded; re-review this head (§17.8 clause 3)"
        )
        raise ReviewVerdictError(msg)
    msg = (
        f"{_INVARIANT} — but the live workflow-run read at the reviewed SHA reports "
        f"{live.status} ({live.detail}), so the envelope contradicts the forge rather than the "
        f"tree contradicting itself. Nothing is recorded; re-review this head (§17.8 clause 3)"
    )
    raise ReviewVerdictError(msg)


__all__ = [
    "ChecksContradictionError",
    "ReviewVerdictError",
    "assert_checks_admit_merge_safe",
    "live_checks_reader",
]
