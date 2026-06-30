"""Decision predicates + model queries for :mod:`teatree.loop.scanners.pr_sweep`.

The scanner core (:class:`PrSweepScanner`, the signal builders) lives in
``pr_sweep``; this module holds the pure check-classification predicates and the
``MergeClear`` / ``ReviewVerdict`` / external-delivery lookups the decision
ladder consults. Splitting them out keeps the scanner module focused on
orchestration and under the module-health LOC cap (same split rationale as
``pr_sweep_adapters``).
"""

import logging
from collections.abc import Iterable

from teatree.core.author_trust import classify_author
from teatree.core.models.merge_clear import MergeClear
from teatree.core.review_candidate import author_is_self
from teatree.loop.pr_ticket_index import resolve_author_ticket
from teatree.loop.scanners.pr_sweep_types import (
    REPO_STATE_CHECK_NAMES,
    REQUIRED_CHECK_NAME,
    UV_AUDIT_CHECK_NAME,
    CheckResult,
    PrSummary,
)

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


def classify_checks(checks: tuple[CheckResult, ...]) -> str:
    """Return ``green`` / ``green_with_uv_audit_red`` / ``pending`` / ``failed``.

    The required check is ``test(3.13)``: if it's not green the PR is not
    mergeable. If it IS green and the ONLY red check is ``uv-audit``, the
    PR falls into the documented fallback path that the scanner is
    authorised to escalate (step 5).
    """
    required = next((c for c in checks if c.name == REQUIRED_CHECK_NAME), None)
    if required is None or required.verdict != "green":
        if any(c.verdict == "pending" for c in checks if c.name == REQUIRED_CHECK_NAME):
            return "pending"
        return "failed" if checks else "pending"
    red = [c for c in checks if c.verdict == "failed"]
    if not red:
        if any(c.verdict == "pending" for c in checks):
            return "pending"
        return "green"
    if all(c.name == UV_AUDIT_CHECK_NAME for c in red):
        return "green_with_uv_audit_red"
    return "failed"


def red_checks_are_all_repo_state(checks: tuple[CheckResult, ...]) -> bool:
    """True iff there is at least one red check and EVERY red check is repo-state (#2045).

    Repo-state checks (``REPO_STATE_CHECK_NAMES``) diff the head against the
    base, so a fix already on ``main`` leaves them red on an un-updated branch
    and a ``gh run rerun`` re-tests the stale base. When every failing check is
    one of these, a merge-update is the remedy. A single non-repo-state red
    (a genuine test failure) makes this ``False`` so the sweep keeps the bare
    ``ci_red`` skip.
    """
    red = [c for c in checks if c.verdict == "failed"]
    return bool(red) and all(c.name in REPO_STATE_CHECK_NAMES for c in red)


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
