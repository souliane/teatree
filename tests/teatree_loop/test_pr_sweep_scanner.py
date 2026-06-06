"""Tests for :class:`PrSweepScanner` — auto-merge-green-PRs sweep (#1248).

The scanner is the structural fix for the "PR sits open for hours after
turning green" failure mode the orchestrator hit on its own merges. It
runs every tick, walks the configured repo list, and merges the PRs
whose ``MergeClear`` row + live CI state pass the BLUEPRINT §17.4.3
pre-conditions. These tests pin every branch of the decision ladder:

* green-and-clean → keystone merge + Slack DM
* draft → skip, no DM
* reviewer changes requested → skip, no DM
* no actionable CLEAR for head SHA → skip
* stale CLEAR (SHA mismatch) → skip
* CI red on a non-uv-audit check → skip
* uv-audit red but ``main`` clean → skip (the fallback only fires
    when the audit job is broken on ``main`` too)
* uv-audit red and ``main`` red on uv-audit → keystone merge with
    ``--fallback-uv-audit`` reason; falls back to ``gh pr merge
    --squash`` iff the keystone refuses on that same path
"""

from dataclasses import dataclass, field, replace

import pytest

from teatree.core.models import AutoReviewDispatch, Task
from teatree.core.models.merge_clear import ClearRequest, MergeClear
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.pr_sweep import CheckResult, PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import (
    AutoReviewTaskDispatcher,
    NullMergeNotifier,
    SlackMergeNotifier,
    _decode_pr,
)

pytestmark = pytest.mark.django_db


SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
STALE = "deadbeef00000000000000000000000000000000"
MAIN_SHA = "abcdef1234567890abcdef1234567890abcdef12"


def _green_required() -> CheckResult:
    return CheckResult(name="test (3.13)", conclusion="SUCCESS", status="COMPLETED")


def _red_uv_audit() -> CheckResult:
    return CheckResult(name="uv-audit", conclusion="FAILURE", status="COMPLETED")


def _red_lint() -> CheckResult:
    return CheckResult(name="lint", conclusion="FAILURE", status="COMPLETED")


def _issue_clear(*, pr_id: int = 6230, sha: str = HEAD) -> MergeClear:
    return MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug=SLUG,
            reviewed_sha=sha,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="logic",
        )
    )


def _open_pr(
    *,
    pr_id: int = 6230,
    head: str = HEAD,
    is_draft: bool = False,
    changes_requested: bool = False,
    checks: tuple[CheckResult, ...] = (),
) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=pr_id,
        head_sha=head,
        is_draft=is_draft,
        has_changes_requested=changes_requested,
        checks=checks or (_green_required(),),
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        title=f"PR {pr_id}",
    )


def _conflicted_pr(*, pr_id: int = 6230, checks: tuple[CheckResult, ...] = ()) -> PrSummary:
    base = _open_pr(pr_id=pr_id, checks=checks)
    return replace(base, is_conflicted=True)


def _record_cold_review(*, pr_id: int = 6230, sha: str = HEAD, reviewer: str = "cold-reviewer") -> ReviewVerdict:
    return ReviewVerdict.record(
        pr_id=pr_id,
        slug=SLUG,
        reviewed_sha=sha,
        verdict="merge_safe",
        reviewer_identity=reviewer,
    )


@dataclass(slots=True)
class FakePrApiClient:
    """Mock ``PrApiClient`` — captures calls so tests assert side effects."""

    prs_by_slug: dict[str, list[PrSummary]] = field(default_factory=dict)
    main_uv_audit_red: bool = False
    fallback_succeeds: bool = True
    merge_pr_calls: list[tuple[str, int]] = field(default_factory=list)
    main_check_calls: list[tuple[str, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return list(self.prs_by_slug.get(slug, ()))

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        self.main_check_calls.append((slug, check_name))
        return self.main_uv_audit_red

    def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]:
        self.merge_pr_calls.append((slug, pr_id))
        return self.fallback_succeeds, MAIN_SHA if self.fallback_succeeds else ""


@dataclass(slots=True)
class FakeKeystone:
    """Mock ``MergeKeystone`` — fixed reply for the merge call."""

    merged: bool = True
    merged_sha: str = MAIN_SHA
    error: str = ""
    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
        self.calls.append(clear_id)
        return self.merged, self.merged_sha, self.error


@dataclass(slots=True)
class FakeReviewDispatcher:
    """Mock ``ReviewDispatcher`` — records every enqueue call."""

    calls: list[tuple[str, int, str, str, str]] = field(default_factory=list)
    returns: bool = True

    def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
        self.calls.append((slug, pr_id, head_sha, pr_url, overlay))
        return self.returns


def _scanner(  # noqa: PLR0913 — test helper: each kwarg maps 1:1 to a PrSweepScanner constructor flag the cases vary.
    *,
    api: FakePrApiClient,
    keystone: FakeKeystone,
    notifier: NullMergeNotifier | None = None,
    repos: tuple[str, ...] = (SLUG,),
    solo_overlay: bool = False,
    auto_review_dispatch: bool = False,
    dispatcher: FakeReviewDispatcher | None = None,
) -> tuple[PrSweepScanner, NullMergeNotifier]:
    notifier = notifier or NullMergeNotifier()
    return (
        PrSweepScanner(
            repos=repos,
            api=api,
            keystone=keystone,
            notifier=notifier,
            overlay="teatree",
            solo_overlay=solo_overlay,
            auto_review_dispatch=auto_review_dispatch,
            review_dispatcher=dispatcher,
        ),
        notifier,
    )


class TestGreenAndClean:
    def test_green_clean_pr_with_actionable_clear_merges(self) -> None:
        clear = _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == [int(clear.pk)]
        assert [s.kind for s in signals] == ["pr_sweep.merged"]
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, False)]
        payload = signals[0].payload
        assert payload["merged"] is True
        assert payload["reason"] == "all_green"
        assert payload["overlay"] == "teatree"

    def test_signal_carries_slug_and_pr_id_for_logging(self) -> None:
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(api=api, keystone=FakeKeystone())

        signals = scanner.scan()

        assert signals[0].payload["slug"] == SLUG
        assert signals[0].payload["pr_id"] == 6230


class TestSkipPaths:
    def test_draft_pr_is_skipped_without_keystone_call(self) -> None:
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(is_draft=True)]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert notifier.calls == []
        assert [s.kind for s in signals] == ["pr_sweep.skip"]
        assert signals[0].payload["reason"] == "draft"

    def test_changes_requested_review_blocks_merge(self) -> None:
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(changes_requested=True)]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "changes_requested"

    def test_no_clear_for_head_skips_without_dm(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert notifier.calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_clear_for_stale_sha_does_not_match_current_head(self) -> None:
        _issue_clear(sha=STALE)
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(head=HEAD)]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_ci_red_on_non_uv_audit_blocks_merge(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_lint()))]},
        )
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "ci_red"

    def test_required_check_not_green_blocks_merge(self) -> None:
        _issue_clear()
        red_required = CheckResult(name="test (3.13)", conclusion="FAILURE", status="COMPLETED")
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(checks=(red_required,))]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "ci_red"


class TestUvAuditFallback:
    def test_uv_audit_red_with_clean_main_skips(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()))]},
            main_uv_audit_red=False,
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert notifier.calls == []
        assert signals[0].payload["reason"] == "uv_audit_red_but_clean_on_main"

    def test_uv_audit_red_with_main_red_keystone_merges(self) -> None:
        clear = _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()))]},
            main_uv_audit_red=True,
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == [int(clear.pk)]
        assert api.merge_pr_calls == []  # keystone took it; no gh fallback
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, True)]
        assert signals[0].payload["reason"] == "fallback_uv_audit"

    def test_keystone_refuses_uv_audit_fallback_escalates_to_gh(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()))]},
            main_uv_audit_red=True,
            fallback_succeeds=True,
        )
        keystone = FakeKeystone(merged=False, error="uv-audit failing", merged_sha="")
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert api.merge_pr_calls == [(SLUG, 6230)]
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, True)]
        assert signals[0].kind == "pr_sweep.merged"
        assert signals[0].payload["reason"] == "fallback_uv_audit_gh"

    def test_keystone_refuses_non_fallback_path_does_not_escalate(self) -> None:
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone(merged=False, error="head moved", merged_sha="")
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        # Non-fallback path: keystone refusal blocks; no raw gh escalation.
        assert api.merge_pr_calls == []
        assert notifier.calls == []
        assert signals[0].kind == "pr_sweep.blocked"
        assert "head moved" in signals[0].payload["reason"]


class TestMultiRepo:
    def test_sweep_walks_each_configured_repo(self) -> None:
        other = "example-org/example-repo"
        _issue_clear()
        pr_a = _open_pr()
        pr_b = PrSummary(
            slug=other,
            number=147,
            head_sha=HEAD,
            is_draft=True,
            has_changes_requested=False,
            checks=(_green_required(),),
        )
        api = FakePrApiClient(prs_by_slug={SLUG: [pr_a], other: [pr_b]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, repos=(SLUG, other))

        signals = scanner.scan()

        kinds = [s.kind for s in signals]
        assert kinds == ["pr_sweep.merged", "pr_sweep.skip"]
        assert signals[1].payload["slug"] == other
        assert signals[1].payload["reason"] == "draft"


class TestSoloOverlayBypassesClearGate:
    """Solo-overlay bypass — green PRs merge via gh fallback when no CLEAR exists (#1309).

    A solo overlay (single-author repo opted into auto + no-human-approval-to-merge)
    cannot issue a CLEAR for its own PRs — the maker/reviewer is the same identity, and
    ``MergeClear.issue`` refuses self-attested CLEARs. The scanner must still merge
    green+mergeable+clean PRs on those overlays via direct ``gh pr merge --squash``;
    refusing on ``no_clear_for_head`` makes the sweep silently no-op on the dogfood
    overlay.
    """

    def test_solo_overlay_with_cold_review_but_no_clear_merges_via_gh_fallback(self) -> None:
        # No CLEAR (the dogfood case) but a recorded independent cold-review — the
        # solo bypass skips the per-diff CLEAR, never the cold-review requirement.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []  # CLEAR-keystone never invoked
        assert api.merge_pr_calls == [(SLUG, 6230)]  # direct gh fallback fired
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, False)]
        assert notifier.flag_calls == []
        assert [s.kind for s in signals] == ["pr_sweep.merged"]
        assert signals[0].payload["reason"] == "solo_overlay_no_clear"

    def test_solo_overlay_still_skips_draft_prs(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(is_draft=True)]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == []
        assert notifier.calls == []
        assert signals[0].payload["reason"] == "draft"

    def test_solo_overlay_still_skips_on_changes_requested(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(changes_requested=True)]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert signals[0].payload["reason"] == "changes_requested"

    def test_solo_overlay_still_skips_on_ci_red(self) -> None:
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_lint()))]},
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == []
        assert notifier.calls == []
        assert signals[0].payload["reason"] == "ci_red"

    def test_solo_overlay_prefers_existing_clear_when_one_was_issued(self) -> None:
        # When a CLEAR exists (e.g. a colleague did review the solo overlay anyway),
        # the keystone path wins so the audit row gets written through the canonical
        # transition. The fallback is only for the no-CLEAR case.
        clear = _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == [int(clear.pk)]
        assert api.merge_pr_calls == []  # keystone took it, no gh fallback
        assert signals[0].payload["reason"] == "all_green"

    def test_solo_overlay_gh_fallback_failure_emits_blocked_signal(self) -> None:
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]}, fallback_succeeds=False)
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == [(SLUG, 6230)]
        assert notifier.calls == []
        assert signals[0].kind == "pr_sweep.blocked"
        assert signals[0].payload["reason"] == "solo_overlay_gh_fallback_failed"

    def test_collaborative_overlay_default_still_skips_on_no_clear(self) -> None:
        # Anti-vacuous: without solo_overlay, the existing no_clear_for_head
        # skip MUST still fire. This is the gate that keeps the CLEAR contract
        # in force for every overlay that did not explicitly opt in.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=False)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == []
        assert notifier.calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"


class TestSoloOverlayRequiresIndependentColdReview:
    """Gap A (#68): the solo-overlay bypass must require a recorded cold-review.

    The bypass skips only the per-diff CLEAR — never the maker≠checker
    boundary. A green+clean solo-overlay PR with NO recorded independent
    ``ReviewVerdict`` is NOT auto-merged; the scanner emits a flag-level
    signal so the only-identity-on-the-repo maker can never self-merge.
    """

    def test_green_solo_overlay_pr_with_no_cold_review_is_not_merged_and_flags(self) -> None:
        # CI-green, no CLEAR, no recorded cold-review — the maker-self-merge hole.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == []  # the auto-merge was refused
        assert notifier.calls == []  # no merge DM
        assert notifier.flag_calls == [(SLUG, 6230, "no_independent_review", f"https://github.com/{SLUG}/pull/6230")]
        assert [s.kind for s in signals] == ["pr_sweep.flag_no_review"]
        assert signals[0].payload["reason"] == "solo_overlay_no_review"
        assert signals[0].payload["merged"] is False
        assert signals[0].payload["url"] == f"https://github.com/{SLUG}/pull/6230"

    def test_cold_review_for_stale_sha_does_not_authorize_merge(self) -> None:
        # A recorded verdict against a tree the PR has moved off cannot vouch for
        # the live head — the stale row is treated as absent and the PR is flagged.
        _record_cold_review(sha=STALE)
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(head=HEAD)]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert api.merge_pr_calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"

    def test_hold_verdict_does_not_authorize_merge(self) -> None:
        # A recorded HOLD is not a merge-safe verdict — it must not unlock the bypass.
        ReviewVerdict.record(
            pr_id=6230,
            slug=SLUG,
            reviewed_sha=HEAD,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
        )
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert api.merge_pr_calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"

    def test_collaborative_overlay_unaffected_by_cold_review_gate(self) -> None:
        # Anti-vacuous: the cold-review gate is solo-overlay-only. A non-solo
        # overlay with a recorded cold-review still skips on no_clear_for_head —
        # the gate did not silently turn the collaborative default into a bypass.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=False)

        signals = scanner.scan()

        assert api.merge_pr_calls == []
        assert notifier.flag_calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"


class TestAutoReviewDispatch:
    """flag_no_review on a full-autonomy overlay enqueues ONE claimable review task (#68).

    The structural fix for "own CI-green PR sits open because nothing dispatches
    the cold review". When the scanner refuses to self-merge (no independent
    verdict) AND ``auto_review_dispatch`` is on, it enqueues a deduped reviewing
    task whose recorded verdict the NEXT sweep merges on. Red CI / conflict /
    draft never reach this path (the sweep already skips those). A
    human-approval-required overlay (``solo_overlay=False``) never reaches
    ``_flag_no_review`` at all, so it never enqueues.
    """

    def test_flag_no_review_enqueues_one_review_task_when_armed(self) -> None:
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == [(SLUG, 6230, HEAD, f"https://github.com/{SLUG}/pull/6230", "teatree")]
        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is True

    def test_no_dispatch_when_flag_disabled(self) -> None:
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=False, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload.get("review_dispatched") is False

    def test_flag_on_but_no_dispatcher_does_not_dispatch(self) -> None:
        # Defensive: armed but no dispatcher wired -> not dispatched, no crash.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=None
        )

        signals = scanner.scan()

        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is False

    def test_dedup_dispatcher_returns_false_marks_not_dispatched(self) -> None:
        # The dispatcher reports "already armed for this head" (dedup) — the
        # signal records review_dispatched=False so a re-tick is a no-op.
        dispatcher = FakeReviewDispatcher(returns=False)
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == [(SLUG, 6230, HEAD, f"https://github.com/{SLUG}/pull/6230", "teatree")]
        assert signals[0].payload["review_dispatched"] is False

    def test_recorded_verdict_suppresses_dispatch_entirely(self) -> None:
        # An independent merge_safe verdict at the head means the PR merges — it
        # never reaches flag_no_review, so the dispatcher is never called.
        _record_cold_review()
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].kind == "pr_sweep.merged"

    def test_red_ci_suppresses_dispatch(self) -> None:
        # A red required check skips before the cold-review gate — no review task.
        red_required = CheckResult(name="test (3.13)", conclusion="FAILURE", status="COMPLETED")
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(checks=(red_required,))]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].payload["reason"] == "ci_red"

    def test_draft_suppresses_dispatch(self) -> None:
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(is_draft=True)]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].payload["reason"] == "draft"

    def test_human_approval_overlay_never_reaches_dispatch_path(self) -> None:
        # solo_overlay=False is the human-approval-required posture (a
        # GitLab-governed client overlay): the sweep skips on no_clear_for_head
        # and never enters _evaluate_solo_overlay, so no review task is enqueued.
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=False, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_end_to_end_enqueued_task_then_recorded_verdict_merges_on_next_sweep(self) -> None:
        # Sweep 1: no verdict, armed -> flag_no_review + a real reviewing task.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=AutoReviewTaskDispatcher(),
        )

        first = scanner.scan()

        assert first[0].kind == "pr_sweep.flag_no_review"
        assert first[0].payload["review_dispatched"] is True
        assert AutoReviewDispatch.objects.filter(slug=SLUG, pr_id=6230, head_sha=HEAD).count() == 1
        review_task = Task.objects.get(phase="reviewing")
        assert review_task.status == Task.Status.PENDING

        # The reviewer records a merge_safe verdict at the reviewed head.
        _record_cold_review()

        # Sweep 2 (same head): the recorded verdict authorises the merge.
        second = scanner.scan()

        assert second[0].kind == "pr_sweep.merged"
        assert api.merge_pr_calls == [(SLUG, 6230)]
        # No second dispatch row — the verdict path merged instead of re-arming.
        assert AutoReviewDispatch.objects.count() == 1

    def test_dispatcher_failure_does_not_crash_sweep(self) -> None:
        @dataclass(slots=True)
        class _BoomDispatcher:
            def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
                msg = "db down"
                raise RuntimeError(msg)

        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=_BoomDispatcher()
        )

        signals = scanner.scan()

        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is False


class TestConflictFlag:
    """Gap B (#78): a conflicted open PR emits a flag — flag only, never an auto-rebase."""

    def test_conflicted_pr_emits_conflict_flag_without_merging(self) -> None:
        # Even fully green + cleared, a conflicted PR is flagged, not merged.
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_conflicted_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []  # never merged
        assert api.merge_pr_calls == []  # never rebased / squash-merged
        assert notifier.calls == []
        assert notifier.flag_calls == [(SLUG, 6230, "conflict", f"https://github.com/{SLUG}/pull/6230")]
        assert [s.kind for s in signals] == ["pr_sweep.flag_conflict"]
        assert signals[0].payload["reason"] == "conflict"
        assert signals[0].payload["merged"] is False
        assert signals[0].payload["url"] == f"https://github.com/{SLUG}/pull/6230"

    def test_conflicted_solo_overlay_pr_is_flagged_not_merged(self) -> None:
        # The conflict flag precedes the solo bypass — a conflicted PR never
        # reaches the gh fallback even on a full-autonomy overlay.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_conflicted_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert api.merge_pr_calls == []
        assert signals[0].kind == "pr_sweep.flag_conflict"

    def test_non_conflicted_pr_is_not_flagged(self) -> None:
        # Anti-vacuous: a clean (non-conflicted) green+cleared PR still merges,
        # so the flag fires on the conflict, not on every PR.
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert notifier.flag_calls == []
        assert [s.kind for s in signals] == ["pr_sweep.merged"]


class TestGhConflictDecode:
    """The ``gh`` adapter maps GitHub's mergeable / mergeStateStatus to is_conflicted."""

    def test_decode_marks_conflicting_mergeable_as_conflicted(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "headRefOid": HEAD, "mergeable": "CONFLICTING"})

        assert pr.is_conflicted is True

    def test_decode_marks_dirty_merge_state_as_conflicted(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "headRefOid": HEAD, "mergeStateStatus": "DIRTY"})

        assert pr.is_conflicted is True

    def test_decode_does_not_flag_behind_or_unknown_states(self) -> None:
        behind = _decode_pr(slug=SLUG, raw={"number": 1, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"})
        unknown = _decode_pr(slug=SLUG, raw={"number": 2, "mergeable": "UNKNOWN", "mergeStateStatus": ""})

        assert behind.is_conflicted is False
        assert unknown.is_conflicted is False


class TestSlackMergeNotifier:
    """The Slack DM notifier posts on a merge and on a flag-level signal."""

    @dataclass(slots=True)
    class _Backend:
        posts: list[tuple[str, str]] = field(default_factory=list)

        def post_dm(self, *, channel: str, text: str) -> None:
            self.posts.append((channel, text))

    def test_announce_posts_merge_dm(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="U1").announce(
            slug=SLUG, pr_id=42, merged_sha=MAIN_SHA, fallback=False
        )
        assert backend.posts == [("U1", f"merged {SLUG}#42 @ {MAIN_SHA[:8]}")]

    def test_announce_marks_uv_audit_fallback(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="U1").announce(slug=SLUG, pr_id=42, merged_sha="", fallback=True)
        assert backend.posts == [("U1", f"merged (uv-audit fallback) {SLUG}#42 @ ?")]

    def test_flag_posts_clickable_url(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="U1").flag(
            slug=SLUG, pr_id=42, reason="conflict", url="https://github.com/x/pull/42"
        )
        assert backend.posts == [("U1", "flag (conflict) https://github.com/x/pull/42")]

    def test_flag_falls_back_to_slug_when_url_missing(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="U1").flag(
            slug=SLUG, pr_id=42, reason="no_independent_review", url=""
        )
        assert backend.posts == [("U1", f"flag (no_independent_review) {SLUG}#42")]

    def test_no_user_id_posts_nothing(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="").flag(slug=SLUG, pr_id=42, reason="conflict", url="")
        assert backend.posts == []

    def test_backend_without_post_method_is_silent(self) -> None:
        SlackMergeNotifier(backend=object(), user_id="U1").flag(slug=SLUG, pr_id=42, reason="conflict", url="")


class TestErrorIsolation:
    def test_api_failure_does_not_crash_sweep(self) -> None:
        @dataclass(slots=True)
        class _BoomApi:
            calls: int = 0

            def list_open_prs(self, *, slug: str) -> list[PrSummary]:
                self.calls += 1
                msg = "gh broke"
                raise RuntimeError(msg)

            def main_check_failed(self, *, slug: str, check_name: str) -> bool:  # pragma: no cover
                return False

            def merge_pr_squash(self, *, slug: str, pr_id: int) -> tuple[bool, str]:  # pragma: no cover
                return False, ""

        api = _BoomApi()
        scanner = PrSweepScanner(
            repos=(SLUG,),
            api=api,
            keystone=FakeKeystone(),
            notifier=NullMergeNotifier(),
            overlay="teatree",
        )

        signals = scanner.scan()

        assert signals == []
        assert api.calls == 1

    def test_evaluate_failure_on_one_pr_does_not_prevent_sibling_from_merging(self) -> None:
        """A runtime error in _evaluate for PR A must not skip PR B (#1596)."""
        clear_b = _issue_clear(pr_id=7777)
        pr_a = _open_pr(pr_id=6230)
        pr_b = _open_pr(pr_id=7777)

        @dataclass(slots=True)
        class _BoomFirstKeystone:
            calls: list[int] = field(default_factory=list)

            def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
                self.calls.append(clear_id)
                if not self.calls or (self.calls == [clear_id] and len(self.calls) == 1):
                    # First call: inject a fault to simulate a merge conflict / DB error
                    msg = "simulated keystone failure"
                    raise RuntimeError(msg)
                return True, MAIN_SHA, ""  # pragma: no cover

        # Issue a CLEAR for both PRs so _evaluate reaches _merge for each.
        _issue_clear(pr_id=6230)
        keystone = _BoomFirstKeystone()
        api = FakePrApiClient(prs_by_slug={SLUG: [pr_a, pr_b]})
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        # PR A failed — its signal must be absent; PR B succeeded.
        assert len(signals) == 1
        assert signals[0].payload["pr_id"] == int(clear_b.pr_id)
        assert signals[0].kind == "pr_sweep.merged"

    def test_scanner_error_from_evaluate_propagates_out_of_scan(self) -> None:
        """A ScannerError raised inside _evaluate must not be swallowed (#1596)."""
        _issue_clear()

        @dataclass(slots=True)
        class _AuthErrorKeystone:
            def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str]:
                raise ScannerError(
                    scanner="pr_sweep",
                    error_class=ScannerErrorClass.AUTH,
                    detail="token revoked",
                )

        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(api=api, keystone=_AuthErrorKeystone())

        with pytest.raises(ScannerError):
            scanner.scan()

    def test_flag_notifier_failure_does_not_crash_sweep(self) -> None:
        """A notifier whose ``flag`` raises must not abort the conflict flag (#78)."""

        @dataclass(slots=True)
        class _BoomFlagNotifier:
            def announce(self, *, slug: str, pr_id: int, merged_sha: str, fallback: bool) -> None:  # pragma: no cover
                return

            def flag(self, *, slug: str, pr_id: int, reason: str, url: str) -> None:
                msg = "slack down"
                raise RuntimeError(msg)

        api = FakePrApiClient(prs_by_slug={SLUG: [_conflicted_pr()]})
        scanner = PrSweepScanner(
            repos=(SLUG,),
            api=api,
            keystone=FakeKeystone(),
            notifier=_BoomFlagNotifier(),
            overlay="teatree",
        )

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["pr_sweep.flag_conflict"]


class TestEvaluateOne:
    """On-demand single-PR evaluation — the event-driven sweep complement (#2017).

    ``evaluate_one`` is what a freshly recorded ``merge_safe`` verdict triggers so
    the merge does not idle a full tick cadence. It must reuse the identical
    decision ladder ``scan`` runs (no drift) and no-op cleanly when the PR is no
    longer open.
    """

    def test_evaluate_one_merges_green_cold_reviewed_solo_pr_via_gh(self) -> None:
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        attempt = scanner.evaluate_one(slug=SLUG, pr_id=6230)

        assert attempt is not None
        assert attempt.merged is True
        assert api.merge_pr_calls == [(SLUG, 6230)]
        assert attempt.reason == "solo_overlay_no_clear"

    def test_evaluate_one_returns_none_when_pr_no_longer_open(self) -> None:
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: []})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        attempt = scanner.evaluate_one(slug=SLUG, pr_id=6230)

        assert attempt is None
        assert api.merge_pr_calls == []

    def test_evaluate_one_scopes_to_the_target_pr_only(self) -> None:
        _record_cold_review(pr_id=6230)
        other = _open_pr(pr_id=9999)
        api = FakePrApiClient(prs_by_slug={SLUG: [other, _open_pr(pr_id=6230)]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        attempt = scanner.evaluate_one(slug=SLUG, pr_id=6230)

        assert attempt is not None
        assert attempt.pr_id == 6230
        assert api.merge_pr_calls == [(SLUG, 6230)]  # 9999 (no cold review) untouched

    def test_evaluate_one_does_not_merge_without_cold_review(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        attempt = scanner.evaluate_one(slug=SLUG, pr_id=6230)

        assert attempt is not None
        assert attempt.merged is False
        assert attempt.decision == "flag_no_review"
        assert api.merge_pr_calls == []
