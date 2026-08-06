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
    # Tri-state head-branch provenance (#3244): True = same-repo branch (trusted),
    # False = fork / cross-repo (holds for human approval), None = the forge did not
    # report it ⇒ fail closed to the identity+visibility author check.
    same_repo: bool | None = None


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    """The scanner's per-PR decision plus any merge outcome.

    ``failing_required`` and ``base_current`` are the CI facts the decision was
    made on, carried out to the emitted signal so a CROSS-PR comparison (#4090)
    can ask a question about the SET without re-listing and re-classifying every
    PR. Empty / ``True`` on any path that never reached the CI gate.
    """

    slug: str
    pr_id: int
    decision: str
    merged: bool = False
    merged_sha: str = ""
    reason: str = ""
    url: str = ""
    review_dispatched: bool = False
    failing_required: tuple[str, ...] = ()
    base_current: bool = True
