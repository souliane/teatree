"""What the factory DOES about a review finding — fix it, or post about it.

The rule, keyed on the merge request's AUTHOR and nothing else:

* the author is **one of ours** (the operator, or a bot the operator declared as
    themselves) — **FIX IT**. The finding is implemented on the MR's own branch and
    pushed. A comment there merely registers a defect against ourselves;
* **any other author** — **POST**. The finding is surfaced as a review comment and
    the colleague's branch is never touched.

The discriminator is the author because neither the repo nor the branch answers
the question: a colleague can open an MR on our own repo, and we open MRs on
theirs. The identity set is configuration (``user_identity_aliases`` /
``self_forge_identities``, reaching this module as the resolved
``backend.identities``), never a literal in this file — the bot handle has changed
before. It resolves through :func:`author_is_self`, the ONE self-author signal the
merge sweep and the reviewer scanners already share.

It fails SAFE: an author that cannot be resolved — blank payload, an identity set
we could not read — is NOT ours, so the disposition is POST. A branch is never
written to on an unresolved identity.

A finding reaches this module as one of two signals:

* a ``review_verdict`` of ``hold`` is a
    :class:`~teatree.core.models.review_verdict.ReviewVerdict` row, read at the PR's
    live head through :class:`ReviewVerdictReader`;
* an inline note on the forge bumps ``user_notes_count``, which
    :class:`~teatree.loop.scanners.my_prs.MyPrsScanner` carries as the
    ``my_pr.draft_notes`` signal.

With the FIX disposition the signal routes to the EXISTING fix lane (``t3:debug``
-> ``persistence._handle_debug`` -> a ``debugging`` task, deduped per head by
:class:`~teatree.core.models.RedMrFixAttempt`) that CI-redness and merge conflicts
already use.

The verdict read is a port, not a direct query, for the same reason
``MyPrsScanner.ci_enricher`` is: the scanner stays free of the ORM, and a caller
that wires no reader degrades to the forge note count rather than losing the lane.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict
from teatree.core.review.review_candidate import author_is_self
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


class FindingDisposition(Enum):
    """What the factory does with a finding on a merge request it reviewed."""

    #: Our own (or our bot's) MR — implement the finding on its branch and push.
    FIX = "fix"
    #: Anyone else's MR — surface it as a review comment; never touch the branch.
    POST = "post"


def disposition_for_author(author: str, *, self_identities: Iterable[str]) -> FindingDisposition:
    """:attr:`FindingDisposition.FIX` iff *author* is provably one of ours.

    Everything else — a colleague, an empty author, an unreadable identity set —
    is :attr:`FindingDisposition.POST`. That asymmetry is the safety property: the
    FIX branch writes to somebody's branch, so it may only be reached on a positive
    identity match, never on the absence of a negative one.
    """
    identities = tuple(self_identities)
    if not author or not identities:
        return FindingDisposition.POST
    if author_is_self(author, current_user=identities[0], self_identities=identities):
        return FindingDisposition.FIX
    return FindingDisposition.POST


class ReviewVerdictReader(Protocol):
    """Resolves the EFFECTIVE (newest-wins) recorded verdict state at a PR's live head."""

    def state_for(self, *, url: str, head_sha: str) -> HeadVerdictState | None: ...


@dataclass(slots=True)
class RecordedVerdictReader:
    """Production :class:`ReviewVerdictReader` over the recorded ``ReviewVerdict`` rows.

    Shares ``ReviewVerdict.objects.effective_state_at`` with the merge gate and the
    solo-sweep predicate, so the fix lane and the merge gate can never disagree
    about whether a head is held. An unparsable URL, a blank head, or a DB error
    resolves to ``None`` — "no verdict knowledge", which hands the decision back to
    the forge note count rather than aborting the tick.
    """

    def state_for(self, *, url: str, head_sha: str) -> HeadVerdictState | None:  # noqa: PLR6301 — instance method satisfies the injected ReviewVerdictReader Protocol (mirrors the sibling scanner ports).
        ref = pr_ref_from_url(url)
        if ref is None or not head_sha:
            return None
        try:
            return ReviewVerdict.objects.effective_state_at(slug=ref.slug, pr_id=ref.pr_id, head_sha=head_sha)
        except Exception:
            logger.exception("pr_findings could not read the recorded verdict state for %s", url)
            return None


def unaddressed_review_findings(*, verdict_state: HeadVerdictState | None, notes_count: int) -> bool:
    """Whether this head still owes work for review findings.

    One rule per state, because each state means something different:

    * ``HOLD`` — a cold reviewer looked at THIS head and blocked it. That is a
        finding whether or not anything was posted on the forge: the headless
        reviewer is Bash-denied and RETURNS its findings, so a HOLD frequently
        carries no note at all and the note count alone misses it;
    * ``MERGE_SAFE`` — a cold reviewer looked at THIS head and vouched for it, so
        the notes on it are discharged or were never findings. Acting here would
        burn a dispatch on a PR that is ready to merge;
    * ``NO_MERGE_SAFE`` / ``None`` — nobody has judged this head. The forge's own
        note count is the only evidence there is, so it decides.
    """
    if verdict_state is HeadVerdictState.HOLD:
        return True
    if verdict_state is HeadVerdictState.MERGE_SAFE:
        return False
    return notes_count > 0


__all__ = [
    "FindingDisposition",
    "RecordedVerdictReader",
    "ReviewVerdictReader",
    "disposition_for_author",
    "unaddressed_review_findings",
]
