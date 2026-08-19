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

# The reason a PR carries when a CLEAR for it EXISTS but cannot authorise the live
# head (issued against a since-superseded tree, or missing a load-bearing field).
# Distinct from the no-CLEAR reasons on purpose: reporting an absent authorisation
# as a verdict about review named the wrong cause and hid a re-issuable CLEAR
# behind a log line for as long as it existed (#4249). Owner-audience — see
# ``OWNER_ESCALATION_FLAG_REASONS`` in ``pr_sweep_adapters``.
CLEAR_PRESENT_UNUSABLE_REASON = "clear_present_unusable"

# The reason a PR carries when a HOLD stands at its live head that nobody took back
# AND a non-stale ``merge_safe`` from a DIFFERENT reviewer stands beside it — in either
# recording order, since which row is newer decides what a merge may rest on, not whether
# two reviewers disagree. Two reviewers disagreeing at one unchanged tree is not a verdict
# the loop may resolve by timestamp, so the autonomous no-CLEAR merge refuses and reports
# instead (#4380). Owner-audience — see ``OWNER_ESCALATION_FLAG_REASONS`` in
# ``pr_sweep_adapters``.
CONTESTED_HOLD_REASON = "contested_hold_at_head"

# The same refusal where NO merge_safe stands beside the hold — the ordinary outcome
# of every cold review that holds, and the far more common shape (68 hold-only vs 15
# contested head-groups over the last 400 recorded verdicts). Nothing is in dispute:
# the auto-merge simply has no authorisation. Reported under its own reason so the
# owner DM cannot claim two reviewers disagreed where there is one verdict. Escalates
# to the owner exactly like the contested case — only the wording differs.
HOLD_AT_HEAD_REASON = "hold_at_head"


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

    ``behind_main`` is "the base branch has advanced past this PR's merge base" - a
    fact about TWO COMMITS, decided by
    :func:`~teatree.loop.scanners.pr_sweep_adapters._gh_is_behind_main` from the
    payload's ``baseRefOid`` against the base branch's live head. It is deliberately
    NOT read from ``mergeStateStatus``, which reports only the highest-precedence
    blocker and so says ``BLOCKED`` (never ``BEHIND``) for the behind-AND-red PR the
    stale-base remedy exists to repair (#4526). It is independent of
    ``is_conflicted``: a conflicted PR is normally behind as well, and refusing to
    merge-update that one is the separate conflict bound's job, not this field's.

    Both readers take the same widened meaning. The stale-base remedy repairs or
    flags the PR; the ``mergeable_awaiting_review`` DM withholds a readiness claim it
    can no longer make - which is what its "up-to-date" precondition always said and
    the old predicate silently under-delivered. An UNREADABLE comparison reads as
    ``True`` at both: repairing-or-flagging beats dropping the remedy, and withholding
    one tick's DM costs nothing, because ``MergeableNotified`` records only a DM that
    was actually sent, so the next tick re-offers it.
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
class HeadReview:
    """What the recorded cold-review verdicts say about one PR head (#4380).

    ``held_verdicts`` are the non-stale HOLDs nobody took back, as
    ``(row id, normalised reviewer identity)`` pairs — a non-empty tuple is what
    refuses the autonomous no-CLEAR merge. ``authorizing_verdict`` is what a merge
    records as its authorisation, so it is gated on newest-wins. ``standing_merge_safe``
    is the PASS standing beside a hold: the same row when the PASS is newer, and the row
    newest-wins discards when the HOLD is. They are separate fields because naming a
    two-reviewer disagreement must not depend on which reviewer recorded last.
    """

    held_verdicts: tuple[tuple[int, str], ...] = ()
    authorizing_verdict: tuple[int, str] | None = None
    standing_merge_safe: tuple[int, str] | None = None

    @property
    def hold_reason(self) -> str:
        """The flag reason for a held head — read only when something is holding."""
        return CONTESTED_HOLD_REASON if self.standing_merge_safe else HOLD_AT_HEAD_REASON

    @property
    def hold_detail(self) -> str:
        """The verdicts behind the flag, named so the owner DM need not assert them."""
        holding = ", ".join(f"#{row_id} {reviewer}" for row_id, reviewer in self.held_verdicts)
        if self.standing_merge_safe is None:
            return f"holding: {holding}"
        row_id, reviewer = self.standing_merge_safe
        return f"holding: {holding}; merge_safe: #{row_id} {reviewer}"


@dataclass(frozen=True, slots=True)
class MergeAttempt:
    """The scanner's per-PR decision plus any merge outcome.

    ``failing_required`` and ``base_current`` are the CI facts the decision was
    made on, carried out to the emitted signal so a CROSS-PR comparison (#4090)
    can ask a question about the SET without re-listing and re-classifying every
    PR. Empty / ``True`` on any path that never reached the CI gate.

    ``held_verdicts`` / ``authorizing_verdict`` are the ``(row id, reviewer)`` pairs
    behind a held refusal and the verdict a merge actually relied on (#4380 AC3): a
    bare ``solo_overlay_no_clear`` records that no CLEAR existed but not what was
    relied on instead, which is unanswerable after the fact from the signal alone. A
    HELD attempt merged nothing, so there it carries the PASS standing beside the hold.
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
    held_verdicts: tuple[tuple[int, str], ...] = ()
    authorizing_verdict: tuple[int, str] | None = None
