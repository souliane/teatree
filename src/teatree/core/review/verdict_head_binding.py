"""Which tree a returned review verdict binds to, and when it binds to none (#4126, #4168, #4737).

A verdict is recorded at the head the landed-work guard
(:func:`~teatree.core.models.phase_landing.phase_landing_evidence`) reads, so the head the
reviewer asserts and the head the recorder writes at have to be the same fact. Comparing
the assertion against the DISPATCH head alone made that fact unreachable the moment the
branch advanced: the reviewer judged the PR's live head, the recorder refused it as a
divergence, and — the fingerprint being identical every time — two refusals parked the
phase and the PR stopped being re-dispatched at all.

So the assertion is now measured against BOTH heads the PR can honestly present: the tree
the review was dispatched for, and the tree it points at now. Anything else is still
refused, which is the whole guard — a reviewer that judged neither is a finding, not a
tree to vouch for.
"""

from dataclasses import dataclass

from teatree.core.modelkit.task_failure_taxonomy import HEAD_SUPERSEDED_PREFIX
from teatree.core.review.live_head import LiveHeadProbe, live_head_at
from teatree.utils.pr_ref import PrRef

#: Shortest self-asserted prefix that still identifies a head — git's own abbreviation
#: floor. Anything shorter is read as a divergence, not an abbreviation.
MIN_ABBREVIATED_SHA_LEN = 7


@dataclass(frozen=True, slots=True)
class HeadBinding:
    """The head a returned verdict binds to, or the refusal that stops it binding at all.

    ``superseded`` is the one refusal the REVIEWER could not have avoided: the branch moved
    while it worked, so its claim is spent rather than retried at a tree the PR left behind.
    """

    head: str = ""
    error: str = ""
    superseded: bool = False


def _abbreviates(claimed: str, head: str) -> bool:
    """Whether *claimed* is *head* or a git-length abbreviation of it."""
    return len(claimed) >= MIN_ABBREVIATED_SHA_LEN and head.startswith(claimed)


def resolve_verdict_head(
    *,
    asserted: str,
    dispatch_head: str,
    pr: PrRef,
    read_live_head: LiveHeadProbe | None = None,
) -> HeadBinding:
    """Bind a returned ``reviewed_sha`` to the tree it vouches for, or refuse it.

    The dispatch head binds without touching the forge, so the common path costs no
    round trip. Only an assertion that does NOT match it asks what the PR points at now.

    An OMITTED head is refused (#4168): treating silence as agreement enforced the rule
    only against reviewers that disclose a head, so one that said nothing got ``merge_safe``
    recorded with no check performed at all.
    """
    probe = read_live_head or live_head_at
    claimed = asserted.strip().lower()
    # Bound verbatim, compared case-folded: the bound head is re-compared against the
    # source spelling, so a fold would read an unmoved head as a rebind.
    pinned = dispatch_head.strip()
    if not claimed:
        return HeadBinding(
            error=(
                "review verdict omits reviewed_sha — the head it bound to is undisclosed, so nothing "
                f"was checked against the head this review was dispatched for ({dispatch_head}); the "
                "verdict is not recorded. Return that full 40-char head, which your brief named"
            ),
        )
    if _abbreviates(claimed, pinned.lower()):
        return HeadBinding(head=pinned)

    live = probe(slug=pr.slug, pr_id=pr.pr_id, host_kind=pr.host_kind)
    if live.unreadable or not live.sha:
        return HeadBinding(
            error=(
                f"review verdict reviewed_sha {asserted!r} is not the head this review was dispatched "
                f"for ({dispatch_head}), and the forge could not confirm what {pr.slug}#{pr.pr_id} points at "
                f"now — the verdict is not recorded. Retry the read; the claim is untouched"
            ),
        )
    current = live.sha.strip()
    if _abbreviates(claimed, current.lower()):
        return HeadBinding(head=current)
    if current.lower() != pinned.lower():
        return HeadBinding(
            error=(
                f"{HEAD_SUPERSEDED_PREFIX}{pr.slug}#{pr.pr_id} advanced from {dispatch_head[:8]} to "
                f"{current[:8]} while this review ran, and the reviewer judged {asserted[:8]} — "
                f"neither tree. The verdict is not recorded; review is re-armed at the new head"
            ),
            superseded=True,
        )
    return HeadBinding(
        error=(
            f"review verdict reviewed_sha {asserted!r} is not the head this review was dispatched for "
            f"({dispatch_head}) — a reviewer that judged a different tree than the one it was "
            f"dispatched for is itself a finding; the verdict is not recorded"
        ),
    )


__all__ = ["MIN_ABBREVIATED_SHA_LEN", "HeadBinding", "resolve_verdict_head"]
