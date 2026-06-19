"""Auto-merge-green-PRs scanner (#1248).

For each open PR on a configured repo, the scanner checks the BLUEPRINT
§17.4.3 pre-conditions deterministically and — when they all pass —
invokes the sanctioned ``t3 <overlay> ticket merge <clear_id>``
transition. The orchestrator no longer has to wake up every time a PR
turns green; the loop closes itself.

Decision ladder per open PR:

1. ``mergeable == CONFLICTING`` / ``mergeStateStatus == DIRTY``
    → flag (``pr_sweep.flag_conflict``) — flag only, never an
    auto-rebase (#78)
2. ``draft: true`` → skip
3. open ``CHANGES_REQUESTED`` review → skip
4. no actionable ``MergeClear`` row for ``(slug, pr_id, head_sha)``
    → skip (collaborative-overlay default) OR the solo-overlay
    carve-out (#1309 — see ``solo_overlay`` on :class:`PrSweepScanner`):
    merge via the SHA-bound ``merge_pr_squash_bound`` (#1985) ONLY when a
    recorded independent cold-review (``merge_safe`` ``ReviewVerdict`` at the
    head, ``reviewer != maker``) exists, else flag (``pr_sweep.flag_no_review``,
    #68)
5. CI ``test(3.13)`` not green AND red checks include something
    other than ``uv-audit`` → skip, EXCEPT when every red check is a
    repo-state check (``REPO_STATE_CHECK_NAMES``) AND the branch is BEHIND
    main → ``needs_branch_update`` flag (#2045): a ``gh run rerun`` re-tests
    the run's pinned OLD base, so only a fresh merge-update can clear it.
6. only red check is ``uv-audit`` AND ``main`` is also red on
    ``uv-audit`` → ``--fallback-uv-audit``
7. all required checks green → merge through the keystone

Step 6's ``--fallback-uv-audit`` switch documents the scanner's standing
authorisation to escalate to the SHA-bound ``merge_pr_squash_bound`` when the
keystone transition refuses on the same fallback path (a pre-existing-on-``main``
failing audit job is a deterministic gate, not an ad-hoc judgement —
exactly the case §17.4.3 step 7 reserves for the scanner).

Step 5's ``needs_branch_update`` is a surface-only remedy: the sweep operates
over ``gh`` reads + the keystone merge with no local checkout, so it flags the
``git merge origin/main`` remedy rather than auto-pushing it.

The scanner posts a Slack DM only on actual merges (acceptance gate) and
on a flag-level signal; ordinary skips log to the periodic-task log but
never DM, to keep the DM channel quiet.
"""

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from teatree.core.models.merge_clear import MergeClear
from teatree.loop.scanners.base import ScannerError, ScanSignal
from teatree.loop.scanners.pr_sweep_decision import (
    classify_checks,
    find_actionable_clear,
    has_independent_cold_review,
    pr_authored_by_self,
    pr_ticket_under_external_delivery,
    record_mergeable_notified,
    red_checks_are_all_repo_state,
)
from teatree.loop.scanners.pr_sweep_types import (
    GH_CONFLICT_MERGE_STATE,
    GH_CONFLICT_MERGEABLE,
    GREEN_TERMINAL_CONCLUSIONS,
    MERGEABLE_AWAITING_REVIEW_REASON,
    REPO_STATE_CHECK_NAMES,
    REQUIRED_CHECK_NAME,
    UV_AUDIT_CHECK_NAME,
    CheckResult,
    MergeAttempt,
    PrSummary,
)

__all__ = [
    "GH_CONFLICT_MERGEABLE",
    "GH_CONFLICT_MERGE_STATE",
    "GREEN_TERMINAL_CONCLUSIONS",
    "MERGEABLE_AWAITING_REVIEW_REASON",
    "REPO_STATE_CHECK_NAMES",
    "REQUIRED_CHECK_NAME",
    "UV_AUDIT_CHECK_NAME",
    "CheckResult",
    "MergeAttempt",
    "PrSummary",
    "PrSweepScanner",
]

logger = logging.getLogger(__name__)


@runtime_checkable
class PrApiClient(Protocol):
    """Adapter over ``gh`` used by the scanner — mockable in tests.

    Two methods only: list open PRs on a repo, and fetch the per-PR
    detail block (head SHA, draft, reviews, checks). The implementation
    shells out to ``gh`` with an optional ``GH_TOKEN`` override so each
    overlay can hit its private repos under its own PAT.
    """

    def list_open_prs(self, *, slug: str) -> list[PrSummary]: ...  # pragma: no branch

    def main_check_failed(self, *, slug: str, check_name: str) -> bool: ...  # pragma: no branch

    def merge_pr_squash_bound(
        self,
        *,
        slug: str,
        pr_id: int,
        expected_head_oid: str,
    ) -> tuple[bool, str]: ...  # pragma: no branch


@runtime_checkable
class MergeKeystone(Protocol):
    """Adapter over ``call_command('ticket', 'merge', ...)`` — mockable."""

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
        """Return ``(merged, merged_sha, error)`` — ``error`` is the rejection reason."""
        ...  # pragma: no branch


@runtime_checkable
class ReviewDispatcher(Protocol):
    """Enqueue ONE claimable reviewing task for a no-review own PR (#68) — mockable.

    The production adapter records an
    :class:`teatree.core.models.auto_review_dispatch.AutoReviewDispatch` row
    (deduped per ``(slug, pr_id, head_sha)``) and creates the
    ``Task(phase=reviewing)`` the loop self-pump dispatches to ``t3:reviewer``.
    Returns ``True`` when a new task was armed, ``False`` when a task for this
    head already exists (the dedup no-op).
    """

    def enqueue(
        self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str
    ) -> bool: ...  # pragma: no branch


@runtime_checkable
class MergeNotifier(Protocol):
    """Post a Slack DM on an actual merge, and on a flag-level signal.

    ``announce`` is the merge acceptance gate (a DM only when a merge
    lands). ``flag`` is the optional Slack mirror for a flag-level signal
    the scanner refuses to act on autonomously — a conflicted open PR, or
    a green solo-overlay PR with no recorded independent cold-review. The
    statusline always carries the flag; the Slack DM is the optional
    escalation rung, mirroring the ``forgotten_merge`` detector ladder.
    """

    def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None: ...  # pragma: no branch

    def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None: ...  # pragma: no branch


@dataclass(slots=True)
class PrSweepScanner:
    """Sweep open PRs on configured repos; merge the green-and-cleared ones (#1248).

    *repos* is the ordered list of GitHub ``owner/repo`` slugs the scanner
    sweeps every tick. *api* fetches PR state through ``gh``; *keystone*
    executes the sanctioned merge transition; *notifier* posts the
    post-merge DM (no DM on skips — that's the noise the spec rules out).
    *overlay* tags emitted signals so a multi-overlay loop can attribute
    merges to the right overlay (private-overlay PRs run under a
    different code-host token).

    *solo_overlay* opts the scanner into the dogfood-overlay bypass (#1309).
    A solo overlay is a single-author repo whose user has explicitly opted
    in via ``mode = "auto"`` + ``require_human_approval_to_merge = false``.
    On such an overlay the maker / reviewer is the same human identity, and
    :meth:`MergeClear.issue` mechanically refuses a self-attested CLEAR
    (``is_non_reviewer_role`` guard) — no orchestrator can ever issue a
    CLEAR for that PR. Without this bypass the sweep silently no-ops every
    green+mergeable+clean PR on the dogfood overlay with reason
    ``no_clear_for_head``, which is exactly the failure mode #1309
    reports. When ``solo_overlay=True`` AND no actionable CLEAR exists for
    the head, the scanner runs the same precondition checks (draft,
    changes-requested, CI verdict) and — only if every gate is green —
    falls back to the SHA-bound merge via
    :meth:`PrApiClient.merge_pr_squash_bound`. The CLEAR contract is left
    untouched for every overlay that did NOT explicitly opt in; this is
    the conservative side of the two options on the table because it
    keeps the cold-reviewer attestation as the default and only relaxes
    it for the overlay configuration the user has already declared
    "trust the agent end-to-end".

    Even on a solo overlay the bypass is gated on a recorded INDEPENDENT
    cold-review: a :class:`teatree.core.models.review_verdict.ReviewVerdict`
    that is ``merge_safe``, bound to the live head SHA, and whose reviewer
    is not the maker/coding-agent/loop (the ``ReviewVerdict.record`` factory
    refuses a self-attested verdict via ``is_non_reviewer_role``). With no
    such record the scanner does NOT auto-merge — it emits a flag-level
    signal (``decision=flag_no_review``) so a maker can never self-merge by
    being the only identity on the repo.

    A conflicted open PR (GitHub ``mergeable == CONFLICTING`` or
    ``mergeStateStatus == DIRTY``) is surfaced as a flag-level signal
    (``decision=flag_conflict``) — FLAG ONLY, never an auto-rebase. The
    scanner reads the conflict state from the same ``gh pr list --json``
    call that already drives the merge decision.
    """

    repos: tuple[str, ...]
    api: PrApiClient
    keystone: MergeKeystone
    notifier: MergeNotifier
    overlay: str = ""
    solo_overlay: bool = False
    #: #68: on ``flag_no_review`` for an own CI-green PR, enqueue ONE claimable
    #: reviewing task so the loop dispatches the cold review whose recorded
    #: verdict the next sweep merges on. Only meaningful on the solo-overlay
    #: path (full autonomy + ``require_human_approval_to_merge=false``) — set
    #: by ``scanner_factories`` exactly there; a human-approval overlay never enters
    #: ``_evaluate_solo_overlay`` so it is never armed here in practice.
    auto_review_dispatch: bool = False
    review_dispatcher: "ReviewDispatcher | None" = None
    #: #2210: the operator's own forge identities. The review-arm is scoped to
    #: PRs authored by one of these — ``list_open_prs`` returns colleagues' PRs
    #: too, and a colleague's PR must never be auto-scheduled for review. Empty
    #: means no PR is confirmable as ours, so nothing is armed (fail closed).
    self_identities: tuple[str, ...] = ()
    name: str = "pr_sweep"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for slug in self.repos:
            for pr in self._safe_list(slug):
                try:
                    attempt = self._evaluate(pr)
                except ScannerError:
                    raise  # auth/rate-limit escalation (#1287) — surface to the dispatcher
                except Exception:
                    logger.exception("pr_sweep failed to evaluate %s#%s", slug, getattr(pr, "number", "?"))
                    continue
                self._log_attempt(attempt)
                signals.append(_signal_from_attempt(attempt, overlay=self.overlay))
        return signals

    def evaluate_one(self, *, slug: str, pr_id: int) -> MergeAttempt | None:
        """Run the same decision ladder for a single open PR, on demand (#2026).

        The event-driven complement to the periodic :meth:`scan`: when a
        ``merge_safe`` :class:`ReviewVerdict` is recorded for a PR the sweep
        is waiting on, the merge must not idle a full tick cadence (a parallel
        human merge wins that race). Fetches the one PR through the same
        ``list_open_prs`` adapter and runs the identical :meth:`_evaluate`, so
        the on-demand path and the periodic sweep can never drift. Returns the
        :class:`MergeAttempt` (``None`` when the PR is no longer open, so a
        merged / closed PR is a quiet no-op rather than an error).
        """
        pr = next((candidate for candidate in self._safe_list(slug) if candidate.number == pr_id), None)
        if pr is None:
            return None
        attempt = self._evaluate(pr)
        self._log_attempt(attempt)
        return attempt

    @staticmethod
    def _log_attempt(attempt: MergeAttempt) -> None:
        logger.info(
            "pr_sweep %s#%d decision=%s reason=%s merged=%s",
            attempt.slug,
            attempt.pr_id,
            attempt.decision,
            attempt.reason,
            attempt.merged,
        )

    def _safe_list(self, slug: str) -> list[PrSummary]:
        try:
            return self.api.list_open_prs(slug=slug)
        except ScannerError:
            # Auth / rate-limit / missing-scope: propagate to the dispatcher
            # so this scanner is recorded in ``report.errors`` and skipped for
            # one tick (#1287). Silently swallowing would mask the failure.
            raise
        except Exception:
            logger.exception("pr_sweep failed to list PRs for %s", slug)
            return []

    def _evaluate(self, pr: PrSummary) -> MergeAttempt:
        if pr.is_conflicted:
            return self._flag_conflict(pr)
        skip_reason = _precondition_skip_reason(pr)
        if skip_reason is not None:
            return _skip(pr, reason=skip_reason)
        clear = find_actionable_clear(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
        if clear is None:
            if self.solo_overlay:
                return self._evaluate_solo_overlay(pr)
            return self._evaluate_no_clear_collaborative(pr)
        return self._evaluate_with_clear(pr, clear)

    def _evaluate_with_clear(self, pr: PrSummary, clear: MergeClear) -> MergeAttempt:
        ci_skip, fallback = self._ci_gate(pr)
        if ci_skip is not None:
            return self._ci_block(pr, reason=ci_skip)
        return self._merge(pr=pr, clear=clear, fallback=fallback)

    #: Skip reasons whose only failing checks are repo-state checks a rerun
    #: against the pinned OLD base can never clear — a merge-update is the
    #: remedy (#2045). ``ci_red`` covers a repo-state ``failed`` rollup (e.g.
    #: blueprint-cross-pr); ``uv_audit_red_but_clean_on_main`` is the
    #: uv-audit-only red whose fix already landed on main.
    _REPO_STATE_BLOCK_REASONS = frozenset({"ci_red", "uv_audit_red_but_clean_on_main"})

    def _ci_block(self, pr: PrSummary, *, reason: str) -> MergeAttempt:
        """Convert a CI-red block into ``needs_branch_update`` when a rerun can't fix it (#2045).

        A block whose only failing checks are repo-state checks (uv-audit,
        blueprint-cross-pr, …) on a branch that is BEHIND main is the
        rerun-can't-fix case: those checks diff the head against the base, the
        fix already merged to main, and ``gh run rerun --failed`` re-tests
        against the run's pinned OLD base. The only remedy is a fresh
        merge-update minting a new merge ref — surfaced here as a flag-level
        ``needs_branch_update`` signal so it is actionable rather than a silent
        skip. Every other red (a genuine test failure, or a repo-state red on
        an already-up-to-date branch a rerun CAN clear) stays a plain skip.
        """
        if reason in self._REPO_STATE_BLOCK_REASONS and pr.behind_main and red_checks_are_all_repo_state(pr.checks):
            return self._flag_needs_branch_update(pr)
        return _skip(pr, reason=reason)

    def _flag_needs_branch_update(self, pr: PrSummary) -> MergeAttempt:
        self._flag(slug=pr.slug, pr_id=pr.number, reason="needs_branch_update", url=pr.url)
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="needs_branch_update",
            reason="needs_branch_update",
            url=pr.url,
        )

    def _ci_gate(self, pr: PrSummary) -> tuple[str | None, bool]:
        """Run the shared CI verdict gate; return ``(skip_reason, is_uv_audit_fallback)``.

        ``skip_reason`` is non-``None`` when the PR must not merge (red /
        pending checks, or a uv-audit-red PR whose ``main`` is clean). When
        it is ``None`` the second element says whether the merge proceeds on
        the documented uv-audit fallback path. Shared by the CLEAR path and
        the solo-overlay bypass so the two gates cannot drift apart.
        """
        check_verdict = classify_checks(pr.checks)
        if check_verdict in {"failed", "pending"}:
            return ("ci_red" if check_verdict == "failed" else "ci_pending"), False
        fallback = check_verdict == "green_with_uv_audit_red"
        if fallback and not self._main_uv_audit_red(slug=pr.slug):
            return "uv_audit_red_but_clean_on_main", False
        return None, fallback

    def _evaluate_solo_overlay(self, pr: PrSummary) -> MergeAttempt:
        """Merge a green+clean+cold-reviewed PR on a solo overlay without a CLEAR (#1309).

        Runs the same CI verdict gate as the CLEAR path so a red or pending
        check still blocks. A green-only-but-uv-audit-red PR escalates the
        same way (``main`` must also be red on uv-audit). The solo bypass
        skips only the per-diff CLEAR — it still requires a recorded
        INDEPENDENT cold-review (a ``merge_safe`` :class:`ReviewVerdict` at
        the live head whose reviewer is not the maker). Without that record
        the scanner refuses to merge and emits a flag-level signal so the
        only-identity-on-the-repo maker can never self-merge. Once both the
        CI gate and the cold-review gate pass, calls
        :meth:`PrApiClient.merge_pr_squash_bound` — the bound merge runs the
        §17.4.3 SHA-bind + not-draft + live-CI re-checks via
        ``execute_bound_merge`` (the keystone CLEAR path can't be used here
        because it needs a CLEAR row, but the SHA-bind primitive applies
        without one, so a force-push in the TOCTOU window can no longer slip an
        unreviewed head through this bypass — #1985).
        """
        ci_skip, fallback = self._ci_gate(pr)
        if ci_skip is not None:
            return self._ci_block(pr, reason=ci_skip)
        if not has_independent_cold_review(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha):
            return self._flag_no_review(pr)
        ok, merged_sha = self.api.merge_pr_squash_bound(
            slug=pr.slug,
            pr_id=pr.number,
            expected_head_oid=pr.head_sha,
        )
        if not ok:
            return MergeAttempt(
                slug=pr.slug,
                pr_id=pr.number,
                decision="blocked",
                reason="solo_overlay_gh_fallback_failed",
            )
        self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=merged_sha, fallback=fallback)
        reason = "solo_overlay_no_clear_uv_audit" if fallback else "solo_overlay_no_clear"
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="merged",
            merged=True,
            merged_sha=merged_sha,
            reason=reason,
        )

    def _evaluate_no_clear_collaborative(self, pr: PrSummary) -> MergeAttempt:
        """Flag a colleague-facing own PR that is green+clean+up-to-date but uncleared.

        The COLLABORATIVE-overlay complement of :meth:`_evaluate_solo_overlay`:
        on a non-solo overlay the sweep cannot auto-merge an uncleared PR — a
        colleague review is the gate (and #2568's chokepoint already disables an
        auto review-REQUEST). But a silent ``no_clear_for_head`` skip leaves the
        user unaware their own PR turned green. When the PR is authored by the
        operator (``self_identities``), CI-green, and NOT behind main (draft /
        conflict / changes-requested are already filtered upstream), DM the user
        the MR link + "mergeable, ready to request review" — exactly ONCE per
        head via the :class:`MergeableNotified` ledger (a re-tick on the same
        head / a ledger error degrades to the quiet ``no_clear_for_head`` skip),
        re-firing only on a new commit. Notify-only: the sweep never requests
        review and never merges. Every other case (colleague author, behind
        main, red/pending CI) falls through to the existing skip.
        """
        ci_skip, _fallback = self._ci_gate(pr)
        if ci_skip is not None:
            return self._ci_block(pr, reason=ci_skip)
        if not pr_authored_by_self(author=pr.author, self_identities=self.self_identities) or pr.behind_main:
            return _skip(pr, reason="no_clear_for_head")
        if not record_mergeable_notified(pr=pr, overlay=self.overlay):
            return _skip(pr, reason="no_clear_for_head")
        self._flag(slug=pr.slug, pr_id=pr.number, reason=MERGEABLE_AWAITING_REVIEW_REASON, url=pr.url)
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="flag_mergeable",
            reason=MERGEABLE_AWAITING_REVIEW_REASON,
            url=pr.url,
        )

    def _flag_conflict(self, pr: PrSummary) -> MergeAttempt:
        """Surface a conflicted open PR — flag only, never an auto-rebase (#78)."""
        self._flag(slug=pr.slug, pr_id=pr.number, reason="conflict", url=pr.url)
        return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="flag_conflict", reason="conflict", url=pr.url)

    def _flag_no_review(self, pr: PrSummary) -> MergeAttempt:
        """Refuse a solo-overlay auto-merge with no recorded cold-review, then arm the review (#68).

        The maker≠checker boundary still forbids a self-merge — that part is
        flag-only. What changes (#68) is the loop no longer just logs: when
        ``auto_review_dispatch`` is on it enqueues ONE claimable reviewing task
        (deduped per head) whose recorded ``merge_safe`` verdict the NEXT sweep
        merges on. Draft / red-CI / conflict never reach here (the sweep skips
        them upstream), so an armed task only ever covers a green+clean own PR.
        """
        self._flag(slug=pr.slug, pr_id=pr.number, reason="no_independent_review", url=pr.url)
        dispatched = self._enqueue_review(pr)
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="flag_no_review",
            reason="solo_overlay_no_review",
            url=pr.url,
            review_dispatched=dispatched,
        )

    def _enqueue_review(self, pr: PrSummary) -> bool:
        """Enqueue the claimable review task for *pr*; return whether one was armed.

        Best-effort: a missing dispatcher, the flag being off, any enqueue error,
        or a PR not authored by us all degrade to ``False`` (the flag-level signal
        still fires) so a DB hiccup never aborts the sweep.

        #2210: scoped to PRs the operator authored. ``list_open_prs`` returns
        every open PR in a watched repo, colleagues' included; auto-scheduling a
        colleague's PR for review wastes a dispatch and risks an unattended
        review note on their work. A non-self / unconfirmable author is not armed.

        #2104: skips the review-arm when the PR's ticket is under active EXTERNAL
        delivery — a hand-dispatched reviewer is already on it. The loop's own FSM
        never stamps that lease, so a genuinely unowned own green PR still arms.
        """
        if not self.auto_review_dispatch or self.review_dispatcher is None:
            return False
        if not pr_authored_by_self(author=pr.author, self_identities=self.self_identities):
            return False
        if pr_ticket_under_external_delivery(slug=pr.slug, pr_id=pr.number, pr_url=pr.url):
            return False
        try:
            return self.review_dispatcher.enqueue(
                slug=pr.slug,
                pr_id=pr.number,
                head_sha=pr.head_sha,
                pr_url=pr.url,
                overlay=self.overlay,
            )
        except Exception:
            logger.exception("pr_sweep failed to enqueue auto-review task for %s#%d", pr.slug, pr.number)
            return False

    def _flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None:
        try:
            self.notifier.flag(slug=slug, pr_id=pr_id, reason=reason, url=url)
        except Exception:
            logger.exception("pr_sweep failed to post flag notification for %s#%d", slug, pr_id)

    def _main_uv_audit_red(self, *, slug: str) -> bool:
        try:
            return self.api.main_check_failed(slug=slug, check_name=UV_AUDIT_CHECK_NAME)
        except Exception:
            logger.exception("pr_sweep failed to fetch main uv-audit status for %s", slug)
            return False

    def _merge(self, *, pr: PrSummary, clear: MergeClear, fallback: bool) -> MergeAttempt:
        merged, merged_sha, error = self.keystone.merge_clear(clear_id=int(clear.pk))
        if merged:
            self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=merged_sha, fallback=fallback)
            return MergeAttempt(
                slug=pr.slug,
                pr_id=pr.number,
                decision="merged",
                merged=True,
                merged_sha=merged_sha,
                reason="fallback_uv_audit" if fallback else "all_green",
            )
        if fallback:
            ok, fallback_sha = self.api.merge_pr_squash_bound(
                slug=pr.slug,
                pr_id=pr.number,
                expected_head_oid=pr.head_sha,
            )
            if ok:
                self._announce_merge(slug=pr.slug, pr_id=pr.number, merged_sha=fallback_sha, fallback=True)
                return MergeAttempt(
                    slug=pr.slug,
                    pr_id=pr.number,
                    decision="merged",
                    merged=True,
                    merged_sha=fallback_sha,
                    reason="fallback_uv_audit_gh",
                )
        return MergeAttempt(
            slug=pr.slug,
            pr_id=pr.number,
            decision="blocked",
            reason=error or "keystone_refused",
        )

    def _announce_merge(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:
        try:
            self.notifier.announce(slug=slug, pr_id=pr_id, merged_sha=merged_sha, fallback=fallback)
        except Exception:
            logger.exception("pr_sweep failed to post merge notification for %s#%d", slug, pr_id)


def _skip(pr: PrSummary, *, reason: str) -> MergeAttempt:
    return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="skip", reason=reason)


def _precondition_skip_reason(pr: PrSummary) -> str | None:
    if pr.is_draft:
        return "draft"
    if pr.has_changes_requested:
        return "changes_requested"
    return None


def _signal_from_attempt(attempt: MergeAttempt, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="pr_sweep.merged" if attempt.merged else f"pr_sweep.{attempt.decision}",
        summary=f"{attempt.slug}#{attempt.pr_id} {attempt.decision} ({attempt.reason})",
        payload={
            "slug": attempt.slug,
            "pr_id": attempt.pr_id,
            "decision": attempt.decision,
            "reason": attempt.reason,
            "merged": attempt.merged,
            "merged_sha": attempt.merged_sha,
            "overlay": overlay,
            "url": attempt.url,
            "review_dispatched": attempt.review_dispatched,
        },
    )
