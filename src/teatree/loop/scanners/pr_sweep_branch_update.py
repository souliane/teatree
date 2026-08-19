"""The stale-base merge-update remedy for the PR sweep (#4063).

A PR whose required checks went red against a base it has since fallen behind
carries an UNKNOWN verdict, not a red one — the fix may already be on ``main``,
unreachable to it, since ``gh run rerun --failed`` re-tests the run's pinned OLD
base. Re-testing at the current base costs one run and is the only way to find
out, so this module applies the merge-update rather than announcing it.

Four bounds keep a genuinely broken PR from being update-looped. ONE ATTEMPT
PER HEAD: :class:`BranchUpdateAttempt` is claimed BEFORE the API call, so a PR
that comes back red at the CURRENT base (same head, no longer behind) is never
touched again and a failed call is not retried. A PER-TICK CAP
(:data:`MAX_BRANCH_UPDATES_PER_TICK`) bounds the blast radius of a base that
moves under many PRs at once. OWN PRS ONLY: an unattended push to a colleague's
branch is never ours to make, so any other author keeps the flag. NOT
CONFLICTED: a hard conflict needs a human resolution, not a merge-update —
``update_pr_branch`` at one either fails or lands a mess. The scanner ladder
already flags a conflicted PR before the CI gate, but since #4526 reads
behind-ness from the two commits rather than from ``mergeStateStatus``, a
``DIRTY`` PR is now correctly BEHIND as well; the bound is stated here so the
refusal is a rule of the remedy, not an accident of call order.

Every refusal degrades to the ``needs_branch_update`` flag, so a remedy the
sweep declines to apply is surfaced for a human rather than dropped.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from teatree.loop.scanners.pr_sweep_decision import pr_authored_by_self
from teatree.loop.scanners.pr_sweep_ports import PrApiClient
from teatree.loop.scanners.pr_sweep_types import MergeAttempt, PrSummary

logger = logging.getLogger(__name__)

__all__ = [
    "BRANCH_UPDATED_DECISION",
    "MAX_BRANCH_UPDATES_PER_TICK",
    "NEEDS_UPDATE_DECISION",
    "RemedyContext",
    "TickBudget",
    "remedy_stale_base",
]

#: Cap on unattended merge-updates per sweep pass. A base that moves under a
#: whole open-PR set would otherwise mint a CI run for each in one tick; the
#: remainder keep the flag and are picked up by later ticks. Global safety
#: constant, identical for every overlay — not a per-overlay policy.
MAX_BRANCH_UPDATES_PER_TICK = 3

BRANCH_UPDATED_DECISION = "branch_updated"
NEEDS_UPDATE_DECISION = "needs_branch_update"

#: The reason carried on an applied update — distinct from the flag's
#: ``needs_branch_update`` so a tick log tells a performed remedy from a
#: surfaced one.
_APPLIED_REASON = "stale_base_merge_updated"

FlagFn = Callable[..., None]


@dataclass(frozen=True, slots=True)
class RemedyContext:
    """The sweep collaborators the remedy needs, bundled per call.

    *flag* is the scanner's exception-safe notifier seam — the remedy routes every
    refusal through it, so a declined update is always surfaced. *self_identities*
    scopes the update to the operator's OWN PRs: an unattended push to a
    colleague's branch is never ours to make. *overlay* attributes the ledger row.
    """

    api: PrApiClient
    flag: FlagFn
    self_identities: tuple[str, ...] = ()
    overlay: str = ""


@dataclass(slots=True)
class TickBudget:
    """The scanner's per-pass merge-update allowance, charged on APPLIED updates only.

    Owned by :class:`~teatree.loop.scanners.pr_sweep.PrSweepScanner` and reset at
    the top of every ``scan()``. Held here rather than as a bare int on the
    scanner so the spend accounting lives beside the rule it bounds.
    """

    remaining: int = MAX_BRANCH_UPDATES_PER_TICK

    def reset(self) -> None:
        self.remaining = MAX_BRANCH_UPDATES_PER_TICK

    def spend(self) -> None:
        self.remaining -= 1


def remedy_stale_base(pr: PrSummary, *, ctx: RemedyContext, budget: TickBudget) -> MergeAttempt:
    """Merge-update *pr* when every bound allows it; otherwise flag the remedy."""
    if pr.is_conflicted:
        return _flag_needs_update(pr, flag=ctx.flag)
    if budget.remaining <= 0 or not pr_authored_by_self(author=pr.author, self_identities=ctx.self_identities):
        return _flag_needs_update(pr, flag=ctx.flag)
    if not _claim_head(pr, overlay=ctx.overlay):
        return _flag_needs_update(pr, flag=ctx.flag)
    if not _apply_update(pr, api=ctx.api):
        return _flag_needs_update(pr, flag=ctx.flag)
    budget.spend()
    logger.info("pr_sweep merge-updated %s#%d at stale base (head %s)", pr.slug, pr.number, pr.head_sha[:8])
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision=BRANCH_UPDATED_DECISION,
        reason=_APPLIED_REASON,
        url=pr.url,
    )


def _claim_head(pr: PrSummary, *, overlay: str) -> bool:
    """Claim this head for one update attempt; ``False`` when already claimed.

    A ledger error degrades to ``False`` (no update, keep the flag) so a DB
    hiccup can never turn the one-attempt-per-head bound off.
    """
    from teatree.core.models.branch_update_attempt import BranchUpdateAttempt  # noqa: PLC0415 — lazy ORM import

    try:
        row = BranchUpdateAttempt.claim(
            slug=pr.slug,
            pr_id=pr.number,
            head_sha=pr.head_sha,
            pr_url=pr.url,
            overlay=overlay,
        )
    except Exception:
        logger.exception("pr_sweep failed to claim branch-update attempt for %s#%d", pr.slug, pr.number)
        return False
    return row is not None


def _apply_update(pr: PrSummary, *, api: PrApiClient) -> bool:
    try:
        return api.update_pr_branch(slug=pr.slug, pr_id=pr.number, expected_head_oid=pr.head_sha)
    except Exception:
        logger.exception("pr_sweep failed to merge-update %s#%d", pr.slug, pr.number)
        return False


def _flag_needs_update(pr: PrSummary, *, flag: FlagFn) -> MergeAttempt:
    flag(slug=pr.slug, pr_id=pr.number, reason=NEEDS_UPDATE_DECISION, url=pr.url)
    return MergeAttempt(
        slug=pr.slug,
        pr_id=pr.number,
        decision=NEEDS_UPDATE_DECISION,
        reason=NEEDS_UPDATE_DECISION,
        url=pr.url,
    )
