"""Decision predicates + model queries for :mod:`teatree.loop.scanners.pr_sweep`.

The scanner core (:class:`PrSweepScanner`, the signal builders) lives in
``pr_sweep``; this module holds the pure check-classification predicates and the
``ReviewVerdict`` / external-delivery lookups the decision ladder consults (the
CLEAR lookup is its own concern, in ``pr_sweep_clear_lookup``). Splitting them out
keeps the scanner module focused on orchestration and under the module-health LOC
cap (same split rationale as ``pr_sweep_adapters``).
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import TYPE_CHECKING

from teatree.core.merge import classify_required_rollup, failing_required_names
from teatree.core.review.author_trust import AuthorSubject, AutonomyGate, TrustVerdict, decide_author_trust
from teatree.core.review.review_candidate import author_is_self
from teatree.loop.pr_ticket_index import resolve_author_ticket
from teatree.loop.scanners.pr_sweep_types import UV_AUDIT_CHECK_NAME, HeadReview, MergeAttempt, PrSummary

if TYPE_CHECKING:
    from teatree.core.models.review_verdict import ReviewVerdict
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def untrusted_merge_provenance(pr: PrSummary) -> bool:
    """True iff *pr*'s head-branch provenance is not trusted to auto-merge (#3244).

    A FORK / cross-repo head (``same_repo is False``) is untrusted even when the
    author is a trusted identity — the strict fork-holds model. A same-repo head
    (``same_repo is True``) is trusted. Unreported provenance (``None``) fails
    closed to the identity+visibility author check. Delegates to the shared
    :func:`decide_author_trust` at the ``MERGE`` gate — the ONE autonomy decision
    issue intake also applies (#3577) — so this rung, the merge keystone and the
    intake gate cannot drift.
    """
    subject = AuthorSubject(slug=pr.slug, author=pr.author, same_repo=pr.same_repo)
    return decide_author_trust(subject, gate=AutonomyGate.MERGE) is TrustVerdict.HUMAN_REVIEW


def pr_authored_by_self(*, author: str, self_identities: Iterable[str]) -> bool:
    """True iff *author* is one of the operator's own forge identities (#2210).

    The loop's review-sweep walks every open PR in a watched repo via
    ``list_open_prs`` — colleagues' PRs included. Only the operator's own PRs
    should be auto-scheduled for a cold review; a colleague's PR is theirs to
    review (auto-scheduling it wastes a dispatch and risks an unattended
    review note on their work). Reuses the single self-author signal
    :func:`teatree.core.review.review_candidate.author_is_self` — an empty *author*
    or an empty identity set never matches, so an unconfirmable author fails
    closed (no arm) rather than being treated as ours.
    """
    identities = tuple(self_identities)
    if not author or not identities:
        return False
    return author_is_self(author, current_user=identities[0], self_identities=identities)


def own_or_same_repo(pr: PrSummary, *, self_identities: tuple[str, ...]) -> bool:
    """True iff *pr* is the operator's own PR OR on a same-repo head branch (#3244).

    A same-repo bot PR (e.g. ``app/github-actions``) is not authored by an operator
    identity yet IS trusted provenance, so the solo cold-review arm covers it too —
    otherwise it never gains the ``merge_safe`` verdict the sweep merges on. A fork
    (``same_repo is False`` / ``None``) is excluded, matching the strict fork-holds rung.
    """
    return pr_authored_by_self(author=pr.author, self_identities=self_identities) or pr.same_repo is True


def classify_sweep_ci(
    rollup: "list[RawAPIDict]",
    required_names: set[str] | None,
    *,
    main_uv_audit_red: Callable[[], bool],
) -> tuple[str | None, bool, set[str]]:
    """The sweep's CI decision: ``(skip_reason, is_uv_audit_fallback, failing_required)``.

    Routes the core green/pending/failed verdict through the SAME
    :func:`teatree.core.merge.classify_required_rollup` the §17.4 keystone uses
    (#12), scoped to the SAME branch-protection required set — so the sweep and the
    keystone can never re-diverge on which checks gate a merge. On top of that
    shared verdict it layers the two sweep-only branches: the uv-audit fallback (the
    ONLY failing required check is ``uv-audit`` AND ``main`` is red on it too, via
    *main_uv_audit_red*) and, upstream, the repo-state remedy in ``_ci_block``.

    A ``None`` *required_names* (indeterminate branch-protection lookup) fails CLOSED
    with the ``required_checks_indeterminate`` skip. ``failing_required`` lets
    ``_ci_block`` tell a repo-state-only red apart from a genuine test failure.
    """
    if required_names is None:
        return "required_checks_indeterminate", False, set()
    verdict = classify_required_rollup(rollup, required_names)
    failing = failing_required_names(rollup, required_names)
    if verdict == "pending":
        return "ci_pending", False, failing
    if verdict == "failed":
        if failing == {UV_AUDIT_CHECK_NAME}:
            if main_uv_audit_red():
                return None, True, failing
            return "uv_audit_red_but_clean_on_main", False, failing
        return "ci_red", False, failing
    return None, False, failing


def with_ci_context(attempt: MergeAttempt, *, pr: PrSummary, failing: set[str]) -> MergeAttempt:
    """Stamp the CI facts a CROSS-PR comparison needs onto *attempt* (#4090).

    The failing REQUIRED set and whether the run judged the CURRENT base are both
    already computed for the merge decision; carrying them out to the signal is
    what lets the set-level report compare PRs without re-listing them and
    running a second, divergent classifier over the same rollups (#12). The PR
    URL rides along so the report can render a clickable ref for a plain skip.
    """
    return replace(
        attempt,
        failing_required=tuple(sorted(failing)),
        base_current=not pr.behind_main,
        url=attempt.url or pr.url,
    )


def red_required_at_stale_base(failing_required: set[str], *, behind_main: bool, conflicted: bool) -> bool:
    """True iff ≥1 REQUIRED check is failing against a base that has MOVED (#4063).

    *failing_required* is the branch-protection-required set that is currently
    failing (:func:`teatree.core.merge.failing_required_names`); *behind_main*
    comes from the ``Ref.compare`` behind-by read (#4526), so it is true for a
    behind branch whatever else is also wrong with it.

    *conflicted* is why that widening is safe: a conflicted branch is behind too,
    and it needs a human resolution, not a merge-update — so it is refused here
    rather than relying on the upstream conflict flag to keep it away.

    What makes a red verdict unreliable is not WHICH check failed but that the run
    judged a base the branch has since fallen behind — so this generalises the
    repo-state-only rule (#2045) it replaces, which is now one instance of it. A
    repo-state check diffs the head against the base directly; a test check re-runs
    against the run's pinned OLD base. Either way a fix that landed on ``main``
    leaves the check red and ``gh run rerun`` cannot clear it, so the PR carries an
    UNKNOWN verdict and only a merge-update resolves it. An UP-TO-DATE branch's red
    IS its own verdict: it stays a bare ``ci_red`` skip, which is what stops a
    genuinely broken PR from being update-looped.
    """
    return bool(failing_required) and behind_main and not conflicted


def has_independent_cold_review(*, slug: str, pr_id: int, head_sha: str) -> bool:
    """True iff the EFFECTIVE (newest-wins) verdict vouches for this exact head (#68, #2829).

    A :class:`teatree.core.models.review_verdict.ReviewVerdict` is the
    durable record of a cold review; ``ReviewVerdict.record`` refuses a
    self-attested verdict (``is_independent_reviewer_identity``), so any row that
    exists was issued by an identity that is not the maker/coding-agent/
    loop. The bypass requires a ``merge_safe`` verdict bound to the live
    head SHA — a stale verdict reviewed a tree the PR no longer points at
    and cannot authorise the merge. A maker who is the only identity on
    the repo therefore cannot self-merge: no independent reviewer means no
    matching row and the auto-merge is refused.

    #2829: defence-in-depth + better UX — returns ``False`` when the EFFECTIVE
    (most-recent non-stale) verdict at the head is a HOLD, so the solo sweep
    FLAGS the PR (``_flag_no_review``) instead of diving into
    ``execute_bound_merge`` to be refused by :func:`assert_review_verdict_gate`.
    Shares ``ReviewVerdict.objects.effective_state_at`` with that gate so the
    newest-wins logic cannot drift between the two.
    """
    from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict  # noqa: PLC0415 — lazy ORM import

    state = ReviewVerdict.objects.effective_state_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
    return state is HeadVerdictState.MERGE_SAFE


def head_review_state(*, slug: str, pr_id: int, head_sha: str) -> HeadReview:
    """What the recorded verdicts say about *head_sha* — the holds, and the authorisation (#4380).

    The extra precondition on the autonomous, no-CLEAR solo-overlay merge, plus the
    record that merge names as its authorisation. A standing hold is NOT the effective
    verdict: a later ``merge_safe`` from a DIFFERENT reviewer wins under newest-wins and
    still leaves the hold standing, which is exactly the contested head that merged
    itself. Only the holding reviewer lifting their own hold, a human CLEAR, or a new
    push clears it.

    Both held shapes refuse and both escalate to the owner; :attr:`HeadReview.hold_reason`
    names them apart because they are different events to the reader. A ``merge_safe``
    standing beside the hold is a real two-reviewer disagreement — necessarily two
    DISTINCT identities, since :meth:`ReviewVerdict.record` is an ``update_or_create`` on
    ``(slug, pr_id, reviewed_sha, reviewer_identity_normalized)``. A hold with none beside
    it is the ordinary outcome of a cold review that holds, and reporting that as a
    disagreement names a second reviewer who does not exist.

    Three lookups, and the middle one is why: ``standing_merge_safe_at`` asks who stands
    beside the hold, ``authorizing_verdict_at`` asks what a merge may rest on, and only
    the second is gated on newest-wins. Asking one question for both made the wording of
    a refusal depend on recording order. It costs ONE extra queryset scan per held-head
    evaluation, on a path that already runs once per open PR per tick.

    No ``try/except`` on purpose — unlike :func:`record_mergeable_notified`,
    where degrading to empty means "stay quiet", here it would mean "no hold,
    go ahead and merge", so a DB hiccup would merge over a hold. The caller's
    existing handler logs and skips the PR for the tick, and the next tick
    retries — the safe direction. :func:`has_independent_cold_review` above
    behaves the same way.
    """
    from teatree.core.models.review_verdict import ReviewVerdict  # noqa: PLC0415 — lazy ORM import

    holds = ReviewVerdict.objects.unreconciled_holds_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
    standing = ReviewVerdict.objects.standing_merge_safe_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
    authorizing = ReviewVerdict.objects.authorizing_verdict_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
    return HeadReview(
        held_verdicts=tuple(_verdict_ref(hold) for hold in holds),
        authorizing_verdict=None if authorizing is None else _verdict_ref(authorizing),
        standing_merge_safe=None if standing is None else _verdict_ref(standing),
    )


def _verdict_ref(verdict: "ReviewVerdict") -> tuple[int, str]:
    return int(verdict.pk), verdict.reviewer_identity_normalized or verdict.reviewer_identity


def pr_ticket_under_external_delivery(*, slug: str, pr_id: int, pr_url: str) -> bool:
    """True iff the PR's AUTHOR ticket carries a live external-delivery lease (#2104).

    The lease is stamped by ``workspace ticket <ISSUE_URL>`` on the author /
    delivery ticket keyed by the ISSUE-tracker URL — never on the PR URL. So the
    review-arm must ask whether the AUTHOR ticket that OWNS this PR holds the
    lease, resolved through the existing PR→author-ticket linkage
    (:func:`resolve_author_ticket`: ``PullRequest`` FK then
    ``Ticket.extra["prs"]``). A PR with no resolvable author ticket (the loop
    has not seen this delivery) is treated as unowned, so the loop arms the
    review as before.
    """
    from teatree.core.models.external_delivery import under_external_delivery  # noqa: PLC0415 — lazy ORM import

    ticket = resolve_author_ticket(slug=slug, pr_id=pr_id, pr_url=pr_url)
    return ticket is not None and under_external_delivery(ticket)


def record_mergeable_notified(*, pr: PrSummary, overlay: str) -> bool:
    """Record the mergeable-DM ledger row for *pr*'s head; return whether to DM.

    The :class:`MergeableNotified` ledger is the idempotency lock for the
    "mergeable, ready to request review" DM: the first sight of a head records a
    row and returns ``True`` (fire the DM); a re-tick on the same head finds the
    existing row and returns ``False`` (no re-DM). A new push (new head SHA)
    records a fresh row and re-fires exactly once. A ledger insert error degrades
    to ``False`` so a DB hiccup never crashes the tick — the caller falls back to
    a quiet skip.
    """
    from teatree.core.models.mergeable_notified import MergeableNotified  # noqa: PLC0415 — deferred: ORM/app-registry

    try:
        row = MergeableNotified.record(
            slug=pr.slug,
            pr_id=pr.number,
            head_sha=pr.head_sha,
            pr_url=pr.url,
            overlay=overlay,
        )
    except Exception:
        logger.exception("pr_sweep failed to record mergeable-notified ledger for %s#%d", pr.slug, pr.number)
        return False
    return row is not None
