"""Leaf data types + check-name constants for the PR-sweep scanner.

Held in a dependency-free leaf module so both the scanner core
(:mod:`teatree.loop.scanners.pr_sweep`) and the decision predicates
(:mod:`teatree.loop.scanners.pr_sweep_decision`) can import them without a
circular edge. ``pr_sweep`` re-exports every name here, so existing
``from teatree.loop.scanners.pr_sweep import PrSummary`` call sites are
unaffected.
"""

from dataclasses import dataclass, field

from teatree.types import RawAPIDict

GREEN_TERMINAL_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
REQUIRED_CHECK_NAME = "test (3.13)"
UV_AUDIT_CHECK_NAME = "uv-audit"

# Repo-state checks diff the PR head against ``origin/main`` (the base),
# so a fix that already merged to ``main`` turns them red on a branch that
# has not been merge-updated. ``gh run rerun --failed`` re-tests against the
# original run's pinned merge commit (the OLD base), so a rerun can never
# turn them green — only a fresh merge-update (``git merge origin/main``)
# minting a new merge ref can. ``uv-audit`` is the same class the step-6
# fallback already singles out; the cross-PR / doc-gate / tree-scan jobs
# share the base-diffing property.
REPO_STATE_CHECK_NAMES = frozenset(
    {
        UV_AUDIT_CHECK_NAME,
        "blueprint-cross-pr",
        "doc-update-gate",
        "banned-terms-tree",
        "overlay-leak-tree",
    }
)

# GitHub surfaces a merge conflict two ways: ``mergeable == "CONFLICTING"``
# and ``mergeStateStatus == "DIRTY"``. Either is a hard conflict (a behind-
# but-clean branch is ``BEHIND``/``MERGEABLE``, never these). ``UNKNOWN`` /
# empty is GitHub still computing mergeability — never flagged, to avoid a
# false conflict alarm on a freshly-pushed head.
GH_CONFLICT_MERGEABLE = "CONFLICTING"
GH_CONFLICT_MERGE_STATE = "DIRTY"

# The flag reason a colleague-facing own PR carries when it is green, clean,
# and up-to-date but has no actionable CLEAR: the sweep cannot auto-merge it
# (a colleague review is the gate) so it DMs the user "mergeable, ready to
# request review" once per head. Shared between the scanner (the signal /
# ledger trigger) and the Slack notifier (the friendly DM text) so the two
# can never drift.
MERGEABLE_AWAITING_REVIEW_REASON = "mergeable_awaiting_review"


@dataclass(frozen=True, slots=True)
class PrSummary:
    """Decoded subset of a PR's ``gh`` payload the sweep needs.

    ``rollup`` holds the RAW ``statusCheckRollup`` entries (CheckRun /
    StatusContext dicts) verbatim so the sweep's CI gate classifies them through
    the SAME :func:`teatree.core.merge.classify_required_rollup` the keystone uses
    — newest-per-name dedupe and branch-protection-required scoping included —
    instead of a divergent sibling classifier (#12). ``author`` is the PR author's
    forge login (GitHub ``author.login``); it scopes the loop's auto-review-arm to
    PRs the user authored so a colleague's open PR in a watched repo is never
    auto-scheduled for review (#2210). Empty when the payload omits the author —
    treated as "not ours".
    """

    slug: str
    number: int
    head_sha: str
    is_draft: bool
    has_changes_requested: bool
    rollup: tuple[RawAPIDict, ...] = field(default_factory=tuple)
    url: str = ""
    title: str = ""
    is_conflicted: bool = False
    behind_main: bool = False
    author: str = ""


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    """The scanner's per-PR decision plus any merge outcome."""

    slug: str
    pr_id: int
    decision: str
    merged: bool = False
    merged_sha: str = ""
    reason: str = ""
    url: str = ""
    review_dispatched: bool = False
