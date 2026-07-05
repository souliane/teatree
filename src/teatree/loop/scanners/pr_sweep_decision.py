"""Decision predicates + model queries for :mod:`teatree.loop.scanners.pr_sweep`.

The scanner core (:class:`PrSweepScanner`, the signal builders) lives in
``pr_sweep``; this module holds the pure check-classification predicates and the
``MergeClear`` / ``ReviewVerdict`` / external-delivery lookups the decision
ladder consults. Splitting them out keeps the scanner module focused on
orchestration and under the module-health LOC cap (same split rationale as
``pr_sweep_adapters``).
"""

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from teatree.core.author_trust import classify_author
from teatree.core.merge import classify_required_rollup, failing_required_names
from teatree.core.models.merge_clear import MergeClear
from teatree.core.review_candidate import author_is_self
from teatree.loop.pr_ticket_index import resolve_author_ticket
from teatree.loop.scanners.pr_sweep_types import REPO_STATE_CHECK_NAMES, UV_AUDIT_CHECK_NAME, PrSummary

if TYPE_CHECKING:
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def untrusted_public_author(pr: PrSummary) -> bool:
    """True iff *pr* is on a PUBLIC repo authored by an untrusted identity (#1773).

    PRIVATE / internal repos return False (no author check — the user owns
    access control). An empty / unknown author on a public repo is untrusted
    (fail-closed). Delegates to the shared :func:`classify_author` so the
    scanners and the merge keystone cannot drift.
    """
    return classify_author(pr.slug, pr.author).untrusted


def pr_authored_by_self(*, author: str, self_identities: Iterable[str]) -> bool:
    """True iff *author* is one of the operator's own forge identities (#2210).

    The loop's review-sweep walks every open PR in a watched repo via
    ``list_open_prs`` — colleagues' PRs included. Only the operator's own PRs
    should be auto-scheduled for a cold review; a colleague's PR is theirs to
    review (auto-scheduling it wastes a dispatch and risks an unattended
    review note on their work). Reuses the single self-author signal
    :func:`teatree.core.review_candidate.author_is_self` — an empty *author*
    or an empty identity set never matches, so an unconfirmable author fails
    closed (no arm) rather than being treated as ours.
    """
    identities = tuple(self_identities)
    if not author or not identities:
        return False
    return author_is_self(author, current_user=identities[0], self_identities=identities)


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


def red_required_all_repo_state(failing_required: set[str]) -> bool:
    """True iff there is ≥1 failing REQUIRED check and EVERY one is repo-state (#2045).

    *failing_required* is the branch-protection-required set that is currently
    failing (:func:`teatree.core.merge.failing_required_names`). Repo-state checks
    (``REPO_STATE_CHECK_NAMES``) diff the head against the base, so a fix already
    on ``main`` leaves them red on an un-updated branch and a ``gh run rerun``
    re-tests the stale base — a merge-update is the remedy. A single non-repo-state
    failing required check (a genuine test failure) makes this ``False`` so the
    sweep keeps the bare ``ci_red`` skip.
    """
    return bool(failing_required) and failing_required <= REPO_STATE_CHECK_NAMES


def find_actionable_clear(*, slug: str, pr_id: int, head_sha: str) -> MergeClear | None:
    """Locate the actionable, SHA-matched CLEAR for *(slug, pr_id, head_sha)*.

    A row whose ``reviewed_sha`` does not match the live PR head is treated
    as absent (the CLEAR was issued against stale code — §17.4.2 binds the
    authorisation to the exact reviewed tree). The keystone transition
    re-validates SHA-match at merge time as well, so even a stale match
    here would be refused — this lookup just keeps the scanner quiet.
    """
    candidates = MergeClear.objects.filter(
        slug=slug,
        pr_id=pr_id,
        consumed_at__isnull=True,
    ).order_by("-issued_at")
    for clear in candidates:
        if clear.reviewed_sha == head_sha and clear.is_actionable():
            return clear
    return None


def has_independent_cold_review(*, slug: str, pr_id: int, head_sha: str) -> bool:
    """True iff the EFFECTIVE (newest-wins) verdict vouches for this exact head (#68, #2829).

    A :class:`teatree.core.models.review_verdict.ReviewVerdict` is the
    durable record of a cold review; ``ReviewVerdict.record`` refuses a
    self-attested verdict (``is_non_reviewer_role``), so any row that
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
    from teatree.core.models.review_verdict import HeadVerdictState, ReviewVerdict  # noqa: PLC0415

    state = ReviewVerdict.objects.effective_state_at(slug=slug, pr_id=pr_id, head_sha=head_sha)
    return state is HeadVerdictState.MERGE_SAFE


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
    from teatree.core.models.external_delivery import under_external_delivery  # noqa: PLC0415

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
    from teatree.core.models.mergeable_notified import MergeableNotified  # noqa: PLC0415

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
