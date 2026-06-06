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
    merge via ``gh pr merge --squash`` ONLY when a recorded independent
    cold-review (``merge_safe`` ``ReviewVerdict`` at the head,
    ``reviewer != maker``) exists, else flag (``pr_sweep.flag_no_review``,
    #68)
5. CI ``test(3.13)`` not green AND red checks include something
    other than ``uv-audit`` → skip
6. only red check is ``uv-audit`` AND ``main`` is also red on
    ``uv-audit`` → ``--fallback-uv-audit``
7. all required checks green → merge through the keystone

Step 6's ``--fallback-uv-audit`` switch documents the scanner's standing
authorisation to escalate to ``gh pr merge --squash`` when the keystone
transition refuses on the same fallback path (a pre-existing-on-``main``
failing audit job is a deterministic gate, not an ad-hoc judgement —
exactly the case §17.4.3 step 7 reserves for the scanner).

The scanner posts a Slack DM only on actual merges (acceptance gate) and
on a flag-level signal; ordinary skips log to the periodic-task log but
never DM, to keep the DM channel quiet.
"""

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from teatree.core.models.merge_clear import MergeClear
from teatree.loop.scanners.base import ScannerError, ScanSignal

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One required-status check on a PR head."""

    name: str
    conclusion: str
    status: str

    @property
    def verdict(self) -> str:
        upper_status = self.status.upper()
        if upper_status and upper_status != "COMPLETED":
            return "pending"
        upper_conclusion = self.conclusion.upper()
        if upper_conclusion in GREEN_TERMINAL_CONCLUSIONS:
            return "green"
        return "failed"


@dataclass(frozen=True, slots=True)
class PrSummary:
    """Decoded subset of a PR's ``gh`` payload the sweep needs."""

    slug: str
    number: int
    head_sha: str
    is_draft: bool
    has_changes_requested: bool
    checks: tuple[CheckResult, ...]
    url: str = ""
    title: str = ""
    is_conflicted: bool = False


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

    def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]: ...  # pragma: no branch


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
    falls back to a direct ``gh pr merge --squash`` via
    :meth:`PrApiClient.merge_pr_squash`. The CLEAR contract is left
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
    #: by ``tick_jobs`` exactly there; a human-approval overlay never enters
    #: ``_evaluate_solo_overlay`` so it is never armed here in practice.
    auto_review_dispatch: bool = False
    review_dispatcher: "ReviewDispatcher | None" = None
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
        clear = _find_actionable_clear(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha)
        if clear is None:
            return self._evaluate_solo_overlay(pr) if self.solo_overlay else _skip(pr, reason="no_clear_for_head")
        return self._evaluate_with_clear(pr, clear)

    def _evaluate_with_clear(self, pr: PrSummary, clear: MergeClear) -> MergeAttempt:
        ci_skip, fallback = self._ci_gate(pr)
        if ci_skip is not None:
            return _skip(pr, reason=ci_skip)
        return self._merge(pr=pr, clear=clear, fallback=fallback)

    def _ci_gate(self, pr: PrSummary) -> tuple[str | None, bool]:
        """Run the shared CI verdict gate; return ``(skip_reason, is_uv_audit_fallback)``.

        ``skip_reason`` is non-``None`` when the PR must not merge (red /
        pending checks, or a uv-audit-red PR whose ``main`` is clean). When
        it is ``None`` the second element says whether the merge proceeds on
        the documented uv-audit fallback path. Shared by the CLEAR path and
        the solo-overlay bypass so the two gates cannot drift apart.
        """
        check_verdict = _classify_checks(pr.checks)
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
        :meth:`PrApiClient.merge_pr_squash` directly — the keystone path
        can't be used here because it requires a CLEAR row.
        """
        ci_skip, fallback = self._ci_gate(pr)
        if ci_skip is not None:
            return _skip(pr, reason=ci_skip)
        if not _has_independent_cold_review(slug=pr.slug, pr_id=pr.number, head_sha=pr.head_sha):
            return self._flag_no_review(pr)
        ok, merged_sha = self.api.merge_pr_squash(slug=pr.slug, pr_id=pr.number)
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

        Best-effort: a missing dispatcher, the flag being off, or any enqueue
        error all degrade to ``False`` (the flag-level signal still fires) so a
        DB hiccup never aborts the sweep.
        """
        if not self.auto_review_dispatch or self.review_dispatcher is None:
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
            ok, fallback_sha = self.api.merge_pr_squash(slug=pr.slug, pr_id=pr.number)
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


def _classify_checks(checks: tuple[CheckResult, ...]) -> str:
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


def _find_actionable_clear(*, slug: str, pr_id: int, head_sha: str) -> MergeClear | None:
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


def _has_independent_cold_review(*, slug: str, pr_id: int, head_sha: str) -> bool:
    """True iff a recorded INDEPENDENT cold-review vouches for this exact head (#68).

    A :class:`teatree.core.models.review_verdict.ReviewVerdict` is the
    durable record of a cold review; ``ReviewVerdict.record`` refuses a
    self-attested verdict (``is_non_reviewer_role``), so any row that
    exists was issued by an identity that is not the maker/coding-agent/
    loop. The bypass requires a ``merge_safe`` verdict bound to the live
    head SHA — a stale verdict reviewed a tree the PR no longer points at
    and cannot authorise the merge. A maker who is the only identity on
    the repo therefore cannot self-merge: no independent reviewer means no
    matching row and the auto-merge is refused.
    """
    from teatree.core.models.review_verdict import ReviewVerdict  # noqa: PLC0415

    candidates = ReviewVerdict.objects.for_pr(slug, pr_id).filter(verdict=ReviewVerdict.Verdict.MERGE_SAFE)
    return any(not verdict.is_stale_at(head_sha) for verdict in candidates)


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
