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
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.models import AutoReviewDispatch, BotPing, MergeableNotified, Task
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
from teatree.loop.substrate_pinger import NotifyWithFallbackSubstratePinger

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _non_substrate_changed_paths():
    """Default the solo-overlay substrate gate to a non-substrate diff.

    The solo-overlay no-CLEAR bypass classifies the PR's changed paths live
    (Finding 2). With no real PR behind the fake ``gh``, an unmocked fetch
    returns ``[]`` and the FAIL-SAFE gate holds — so every existing
    merge-path test would now hold. Default the fetch to a known
    non-substrate path; the substrate / fail-safe cases override it with
    their own ``with patch(...)``.
    """
    with patch(
        "teatree.loop.scanners.pr_sweep_substrate.fetch_pr_changed_paths",
        return_value=["src/teatree/loop/scanners/pr_sweep.py"],
    ):
        yield


SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
STALE = "deadbeef00000000000000000000000000000000"
MAIN_SHA = "abcdef1234567890abcdef1234567890abcdef12"
SELF_LOGIN = "souliane"
COLLEAGUE_LOGIN = "a-teammate"


def _green_required() -> CheckResult:
    return CheckResult(name="test (3.13)", conclusion="SUCCESS", status="COMPLETED")


def _red_uv_audit() -> CheckResult:
    return CheckResult(name="uv-audit", conclusion="FAILURE", status="COMPLETED")


def _red_lint() -> CheckResult:
    return CheckResult(name="lint", conclusion="FAILURE", status="COMPLETED")


def _red_blueprint_cross_pr() -> CheckResult:
    return CheckResult(name="blueprint-cross-pr", conclusion="FAILURE", status="COMPLETED")


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


def _issue_substrate_clear(*, pr_id: int = 6230, sha: str = HEAD) -> MergeClear:
    return MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug=SLUG,
            reviewed_sha=sha,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="substrate",
        )
    )


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _open_pr(  # noqa: PLR0913 — test helper: each kwarg maps 1:1 to a PrSummary field the cases vary.
    *,
    pr_id: int = 6230,
    head: str = HEAD,
    is_draft: bool = False,
    changes_requested: bool = False,
    checks: tuple[CheckResult, ...] = (),
    behind_main: bool = False,
    author: str = SELF_LOGIN,
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
        behind_main=behind_main,
        author=author,
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
    merge_pr_calls: list[tuple[str, int, str]] = field(default_factory=list)
    main_check_calls: list[tuple[str, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return list(self.prs_by_slug.get(slug, ()))

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        self.main_check_calls.append((slug, check_name))
        return self.main_uv_audit_red

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        self.merge_pr_calls.append((slug, pr_id, expected_head_oid))
        return self.fallback_succeeds, MAIN_SHA if self.fallback_succeeds else ""


@dataclass(slots=True)
class FakeKeystone:
    """Mock ``MergeKeystone`` — fixed reply for the merge call."""

    merged: bool = True
    merged_sha: str = MAIN_SHA
    error: str = ""
    escalation_kind: str = ""
    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str, str]:
        self.calls.append(clear_id)
        return self.merged, self.merged_sha, self.error, self.escalation_kind


@dataclass(slots=True)
class FakeSubstratePinger:
    """Mock ``SubstratePinger`` — records every (text, idempotency_key) ping."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def ping(self, *, text: str, idempotency_key: str) -> None:
        self.calls.append((text, idempotency_key))


@dataclass(slots=True)
class FakeReviewDispatcher:
    """Mock ``ReviewDispatcher`` — records every enqueue call."""

    calls: list[tuple[str, int, str, str, str]] = field(default_factory=list)
    returns: bool = True

    def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
        self.calls.append((slug, pr_id, head_sha, pr_url, overlay))
        return self.returns


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _scanner(  # noqa: PLR0913 — test helper: each kwarg maps 1:1 to a PrSweepScanner constructor flag the cases vary.
    *,
    api: FakePrApiClient,
    keystone: FakeKeystone,
    notifier: NullMergeNotifier | None = None,
    repos: tuple[str, ...] = (SLUG,),
    solo_overlay: bool = False,
    auto_review_dispatch: bool = False,
    dispatcher: FakeReviewDispatcher | None = None,
    self_identities: tuple[str, ...] = (SELF_LOGIN,),
    substrate_pinger: FakeSubstratePinger | None = None,
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
            self_identities=self_identities,
            substrate_pinger=substrate_pinger,
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
        # A COLLEAGUE's open PR with no CLEAR is a pure silent skip — the
        # mergeable-DM flag is scoped to the operator's OWN PRs, so a
        # colleague's PR never DMs (it is theirs to merge).
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author=COLLEAGUE_LOGIN)]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert notifier.calls == []
        assert notifier.flag_calls == []
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_clear_for_stale_sha_does_not_match_current_head(self) -> None:
        # A stale-SHA CLEAR is treated as absent regardless of author; a
        # colleague PR keeps this on the pure skip path so the assertion
        # pins the stale-CLEAR=absent logic, not the own-PR mergeable flag.
        _issue_clear(sha=STALE)
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(head=HEAD, author=COLLEAGUE_LOGIN)]})
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

        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]
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


class TestNeedsBranchUpdate:
    """Repo-state-check red on a behind-main branch → needs_branch_update (#2045).

    ``gh run rerun --failed`` re-tests against the run's pinned merge commit
    (the OLD base), so a repo-state check (uv-audit, blueprint-cross-pr, …)
    whose fix already merged to ``main`` can never go green by a rerun. The
    only remedy is a fresh merge-update minting a new merge ref. The sweep
    surfaces that remedy as ``needs_branch_update`` instead of a bare
    ``skip/ci_red`` so it is actionable — and ONLY when the branch is behind
    main (a repo-state red on an up-to-date branch is a genuine failure).
    """

    def test_repo_state_red_on_behind_branch_classifies_needs_branch_update(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_blueprint_cross_pr()), behind_main=True)]},
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].kind == "pr_sweep.needs_branch_update"
        assert signals[0].payload["decision"] == "needs_branch_update"
        assert signals[0].payload["reason"] == "needs_branch_update"
        assert notifier.flag_calls == [(SLUG, 6230, "needs_branch_update", _open_pr().url)]

    def test_uv_audit_red_on_behind_branch_also_classifies_needs_branch_update(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()), behind_main=True)]},
            main_uv_audit_red=False,
        )
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["decision"] == "needs_branch_update"

    def test_genuine_test_failure_still_classifies_ci_red_not_branch_update(self) -> None:
        _issue_clear()
        red_required = CheckResult(name="test (3.13)", conclusion="FAILURE", status="COMPLETED")
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(red_required, _red_blueprint_cross_pr()), behind_main=True)]},
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "ci_red"
        assert notifier.flag_calls == []

    def test_repo_state_red_but_up_to_date_branch_stays_ci_red(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_blueprint_cross_pr()), behind_main=False)]},
        )
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "ci_red"
        assert notifier.flag_calls == []

    def test_non_repo_state_red_on_behind_branch_stays_ci_red(self) -> None:
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_lint()), behind_main=True)]},
        )
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []
        assert signals[0].payload["reason"] == "ci_red"


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
        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]  # direct gh fallback fired
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
        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]
        assert notifier.calls == []
        assert signals[0].kind == "pr_sweep.blocked"
        assert signals[0].payload["reason"] == "solo_overlay_gh_fallback_failed"

    def test_collaborative_overlay_default_never_auto_merges_on_no_clear(self) -> None:
        # Anti-vacuous: without solo_overlay, the CLEAR contract stays in force —
        # the sweep NEVER auto-merges an uncleared PR. An own green+clean PR now
        # DMs "mergeable, ready to request review" (notify-only) instead of a
        # silent skip; the no-merge guarantee is unchanged.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=False)

        signals = scanner.scan()

        assert keystone.calls == []
        assert api.merge_pr_calls == []  # never auto-merged
        assert notifier.calls == []  # not a merge announcement
        assert signals[0].kind == "pr_sweep.flag_mergeable"
        assert signals[0].payload["reason"] == "mergeable_awaiting_review"


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
        # overlay with a recorded cold-review NEVER auto-merges — the gate did
        # not silently turn the collaborative default into a bypass. (The own
        # green PR now flags mergeable, notify-only; no merge happens.)
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=False)

        signals = scanner.scan()

        assert api.merge_pr_calls == []  # never auto-merged on a collaborative overlay
        assert signals[0].kind == "pr_sweep.flag_mergeable"


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
        # GitLab-governed client overlay): the sweep never enters
        # _evaluate_solo_overlay, so no review task is enqueued. The own green
        # PR flags mergeable (notify-only) instead of arming a cold-review.
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=False, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []  # the collaborative path never auto-dispatches a review
        assert signals[0].payload["reason"] == "mergeable_awaiting_review"

    def test_external_delivery_suppresses_review_arm(self) -> None:
        # #2104: the production seam — a hand-dispatched delivery agent ran
        # ``workspace ticket <ISSUE_URL>``, stamping the lease on the AUTHOR
        # ticket keyed by the ISSUE url (NOT the PR url); the ship pipeline
        # records the PR under that ticket's ``extra["prs"]``. The loop must
        # resolve the PR back to that author ticket and NOT arm a duplicate
        # review — the external reviewer is already on it. The flag-level
        # signal still fires.
        from teatree.core.models import Ticket  # noqa: PLC0415
        from teatree.core.models.external_delivery import mark_external_delivery  # noqa: PLC0415

        pr = _open_pr()
        author_ticket = Ticket.objects.create(
            overlay="teatree",
            issue_url=f"https://github.com/{SLUG}/issues/2104",
            extra={"prs": {pr.url: {"draft": False}}},
        )
        mark_external_delivery(author_ticket)
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [pr]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is False

    def test_unowned_green_pr_still_arms_review(self) -> None:
        # #2104 must-still-fire: the same author-ticket-linked PR shape, but the
        # author ticket holds NO external-delivery lease (the loop owns
        # delivery). The review is still armed.
        from teatree.core.models import Ticket  # noqa: PLC0415

        pr = _open_pr()
        Ticket.objects.create(
            overlay="teatree",
            issue_url=f"https://github.com/{SLUG}/issues/2104",
            extra={"prs": {pr.url: {"draft": False}}},
        )
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [pr]})
        scanner, _ = _scanner(
            api=api, keystone=FakeKeystone(), solo_overlay=True, auto_review_dispatch=True, dispatcher=dispatcher
        )

        signals = scanner.scan()

        assert dispatcher.calls == [(SLUG, 6230, HEAD, pr.url, "teatree")]
        assert signals[0].payload["review_dispatched"] is True

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
        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]
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


class TestReviewArmScopedToOwnPrs:
    """The loop review-sweep auto-arms a review ONLY for PRs the operator authored (#2210).

    ``list_open_prs`` returns every open PR in a watched repo — colleagues'
    included. Before #2210 the solo-overlay ``flag_no_review`` path armed a
    reviewing task for ANY green+clean PR with no CLEAR, so a teammate's MR
    in a customer repo was auto-scheduled for a cold review (wasted dispatch +
    an unattended review note on their work). The arm is now gated on
    ``author_is_self``: a colleague's PR is excluded, the operator's own PR is
    still armed.
    """

    def test_colleague_pr_is_not_armed_for_review(self) -> None:
        # RED before the fix: the colleague's green+clean PR with no CLEAR flowed
        # through _evaluate_solo_overlay -> _flag_no_review -> _enqueue_review and
        # armed a reviewing task. The flag-level signal still fires (operator
        # triage), but no review is dispatched on the teammate's PR.
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author=COLLEAGUE_LOGIN)]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=dispatcher,
            self_identities=(SELF_LOGIN,),
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is False

    def test_own_pr_is_still_armed_for_review(self) -> None:
        # The symmetric must-still-fire: a PR authored by the operator (default
        # author=SELF_LOGIN) is armed exactly as before.
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author=SELF_LOGIN)]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=dispatcher,
            self_identities=(SELF_LOGIN,),
        )

        signals = scanner.scan()

        assert dispatcher.calls == [(SLUG, 6230, HEAD, f"https://github.com/{SLUG}/pull/6230", "teatree")]
        assert signals[0].kind == "pr_sweep.flag_no_review"
        assert signals[0].payload["review_dispatched"] is True

    def test_own_pr_under_secondary_alias_is_armed(self) -> None:
        # Multi-identity: a PR authored under a secondary github login is still
        # the operator's own work and is armed (reuses the full identity set).
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author="souliane-alt")]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=dispatcher,
            self_identities=(SELF_LOGIN, "souliane-alt"),
        )

        signals = scanner.scan()

        assert dispatcher.calls == [(SLUG, 6230, HEAD, f"https://github.com/{SLUG}/pull/6230", "teatree")]
        assert signals[0].payload["review_dispatched"] is True

    def test_unknown_author_is_not_armed(self) -> None:
        # Fail closed: a PR whose author the payload omitted is not provably ours,
        # so it is not auto-scheduled for review.
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author="")]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=dispatcher,
            self_identities=(SELF_LOGIN,),
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].payload["review_dispatched"] is False

    def test_colleague_pr_with_recorded_verdict_still_merges(self) -> None:
        # The author scope gates only the review-ARM, not the merge path. A
        # colleague PR with a recorded independent cold-review at head still
        # merges (the verdict is authoritative); the arm gate never runs.
        _record_cold_review()
        dispatcher = FakeReviewDispatcher()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author=COLLEAGUE_LOGIN)]})
        scanner, _ = _scanner(
            api=api,
            keystone=FakeKeystone(),
            solo_overlay=True,
            auto_review_dispatch=True,
            dispatcher=dispatcher,
            self_identities=(SELF_LOGIN,),
        )

        signals = scanner.scan()

        assert dispatcher.calls == []
        assert signals[0].kind == "pr_sweep.merged"


class TestDecodeAuthor:
    """``_decode_pr`` reads the PR author login from ``gh pr list --json author`` (#2210)."""

    def test_author_login_decoded(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "headRefOid": HEAD, "author": {"login": "souliane"}})
        assert pr.author == "souliane"

    def test_missing_author_decodes_to_empty(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "headRefOid": HEAD})
        assert pr.author == ""

    def test_malformed_author_decodes_to_empty(self) -> None:
        pr = _decode_pr(slug=SLUG, raw={"number": 1, "headRefOid": HEAD, "author": "not-a-dict"})
        assert pr.author == ""


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


class TestMergeableAwaitingReviewFlag:
    """A colleague-facing own PR with no CLEAR but green+clean+up-to-date is flagged mergeable.

    On a COLLABORATIVE overlay (NOT solo_overlay) the sweep cannot auto-merge —
    a colleague review is the gate — but a silent ``no_clear_for_head`` skip
    leaves the user unaware the PR is ready. Instead the sweep DMs the user once
    per head ("mergeable, ready to request review") and never auto-requests
    review nor merges. The :class:`MergeableNotified` ledger keeps the DM at
    exactly once per head, re-firing only on a new commit.
    """

    def test_own_green_clean_uptodate_pr_with_no_clear_is_flagged_mergeable(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone)

        signals = scanner.scan()

        assert keystone.calls == []  # never merged — no CLEAR
        assert api.merge_pr_calls == []  # never auto-merged via the bound path
        assert notifier.calls == []  # not a merge announcement
        assert notifier.flag_calls == [
            (SLUG, 6230, "mergeable_awaiting_review", f"https://github.com/{SLUG}/pull/6230")
        ]
        assert [s.kind for s in signals] == ["pr_sweep.flag_mergeable"]
        assert signals[0].payload["reason"] == "mergeable_awaiting_review"
        assert signals[0].payload["merged"] is False
        assert MergeableNotified.objects.filter(slug=SLUG, pr_id=6230, head_sha=HEAD).count() == 1

    def test_dm_fires_once_per_head_second_sweep_is_silent(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone())

        first = scanner.scan()
        second = scanner.scan()

        assert first[0].kind == "pr_sweep.flag_mergeable"
        # second tick on the same head: ledger already recorded -> no re-DM
        assert notifier.flag_calls == [
            (SLUG, 6230, "mergeable_awaiting_review", f"https://github.com/{SLUG}/pull/6230")
        ]
        assert second[0].kind == "pr_sweep.skip"
        assert second[0].payload["reason"] == "no_clear_for_head"
        assert MergeableNotified.objects.filter(slug=SLUG, pr_id=6230).count() == 1

    def test_new_head_refires_the_dm(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone())
        scanner.scan()

        api.prs_by_slug[SLUG] = [_open_pr(head=STALE)]
        signals = scanner.scan()

        assert signals[0].kind == "pr_sweep.flag_mergeable"
        assert notifier.flag_calls == [
            (SLUG, 6230, "mergeable_awaiting_review", f"https://github.com/{SLUG}/pull/6230"),
            (SLUG, 6230, "mergeable_awaiting_review", f"https://github.com/{SLUG}/pull/6230"),
        ]

    def test_colleague_authored_pr_is_not_flagged_mergeable(self) -> None:
        # A colleague's open PR in a watched repo is theirs — never DM it as ours.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(author=COLLEAGUE_LOGIN)]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone())

        signals = scanner.scan()

        assert notifier.flag_calls == []
        assert signals[0].kind == "pr_sweep.skip"
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_behind_main_pr_is_not_flagged_mergeable(self) -> None:
        # Behind-main is not "ready to request review" — not flagged.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(behind_main=True)]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone())

        signals = scanner.scan()

        assert notifier.flag_calls == []
        assert signals[0].kind == "pr_sweep.skip"
        assert signals[0].payload["reason"] == "no_clear_for_head"

    def test_ci_red_pr_with_no_clear_is_not_flagged_mergeable(self) -> None:
        # A red PR is blocked on ci_red before the mergeable check is reached.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_lint()))]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone())

        signals = scanner.scan()

        assert notifier.flag_calls == []
        assert signals[0].kind == "pr_sweep.skip"
        assert signals[0].payload["reason"] == "ci_red"

    def test_solo_overlay_does_not_flag_mergeable(self) -> None:
        # Anti-vacuous: on a solo overlay the no-CLEAR path takes the solo
        # bypass (flag_no_review without a cold-review), never the
        # collaborative mergeable DM. The mergeable flag is the COLLABORATIVE
        # branch only.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        scanner, notifier = _scanner(api=api, keystone=FakeKeystone(), solo_overlay=True)

        signals = scanner.scan()

        assert all(reason != "mergeable_awaiting_review" for _slug, _pr, reason, _url in notifier.flag_calls)
        assert signals[0].kind == "pr_sweep.flag_no_review"


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

    def test_decode_marks_behind_merge_state_as_behind_main(self) -> None:
        behind = _decode_pr(slug=SLUG, raw={"number": 1, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"})
        clean = _decode_pr(slug=SLUG, raw={"number": 2, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"})

        assert behind.behind_main is True
        assert clean.behind_main is False


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

    def test_mergeable_flag_posts_ready_to_request_review_message(self) -> None:
        backend = self._Backend()
        SlackMergeNotifier(backend=backend, user_id="U1").flag(
            slug=SLUG, pr_id=42, reason="mergeable_awaiting_review", url="https://github.com/x/pull/42"
        )
        assert backend.posts == [("U1", "mergeable, ready to request review https://github.com/x/pull/42")]

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

            def merge_pr_squash_bound(  # pragma: no cover
                self, *, slug: str, pr_id: int, expected_head_oid: str
            ) -> tuple[bool, str]:
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

            def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str, str]:
                self.calls.append(clear_id)
                if not self.calls or (self.calls == [clear_id] and len(self.calls) == 1):
                    # First call: inject a fault to simulate a merge conflict / DB error
                    msg = "simulated keystone failure"
                    raise RuntimeError(msg)
                return True, MAIN_SHA, "", ""  # pragma: no cover

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
            def merge_clear(self, *, clear_id: int) -> tuple[bool, str, str, str]:
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
    """On-demand single-PR evaluation — the event-driven sweep complement (#2026).

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
        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]
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
        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]  # 9999 (no cold review) untouched

    def test_evaluate_one_does_not_merge_without_cold_review(self) -> None:
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, _ = _scanner(api=api, keystone=keystone, solo_overlay=True)

        attempt = scanner.evaluate_one(slug=SLUG, pr_id=6230)

        assert attempt is not None
        assert attempt.merged is False
        assert attempt.decision == "flag_no_review"
        assert api.merge_pr_calls == []


class TestSubstrateHoldPing:
    """A HELD substrate merge pings the owner ONCE per diff (ping-and-hold, #3.1)."""

    def test_substrate_hold_pings_once_with_per_diff_key(self) -> None:
        # The anti-vacuity test (a): a substrate refusal from the keystone fires
        # exactly one notify ping with the per-diff idempotency key. Before the
        # fix the held substrate clear was swallowed silently with no ping.
        clear = _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone(merged=False, error="held: substrate change", escalation_kind="substrate")
        pinger = FakeSubstratePinger()
        scanner, _ = _scanner(api=api, keystone=keystone, substrate_pinger=pinger)

        signals = scanner.scan()

        assert signals[0].payload["decision"] == "blocked"
        assert len(pinger.calls) == 1
        text, key = pinger.calls[0]
        assert key == f"substrate-hold:{SLUG}#6230:{clear.reviewed_sha}"
        assert f"{SLUG}#6230" in text

    def test_non_substrate_block_does_not_ping(self) -> None:
        # The anti-vacuity twin (b-adjacent): a NON-substrate keystone refusal
        # never pings — the loop pings ONLY on substrate.
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone(merged=False, error="some other refusal", escalation_kind="")
        pinger = FakeSubstratePinger()
        scanner, _ = _scanner(api=api, keystone=keystone, substrate_pinger=pinger)

        scanner.scan()

        assert pinger.calls == []

    def test_substrate_hold_without_pinger_does_not_crash(self) -> None:
        _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone(merged=False, error="held", escalation_kind="substrate")
        scanner, _ = _scanner(api=api, keystone=keystone, substrate_pinger=None)

        signals = scanner.scan()

        assert signals[0].payload["decision"] == "blocked"

    def test_substrate_clear_in_uv_audit_fallback_holds_and_pings_no_raw_merge(self) -> None:
        # Finding 1 (fail-open): a SUBSTRATE CLEAR whose only red check is uv-audit
        # (and main is also uv-audit-red) lands on the keystone fallback path. When
        # the keystone refuses (substrate hold), the legacy code raw-merged via
        # ``merge_pr_squash_bound`` BEFORE the substrate-ping check — silently
        # bypassing the hold. The fix gates the raw-merge on the CLEAR not being
        # substrate, so a substrate PR HOLDS + pings instead.
        clear = _issue_substrate_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()))]},
            main_uv_audit_red=True,
            fallback_succeeds=True,
        )
        keystone = FakeKeystone(merged=False, error="held: substrate change", escalation_kind="substrate")
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, substrate_pinger=pinger)

        signals = scanner.scan()

        assert api.merge_pr_calls == []  # the raw gh fallback was NOT fired for substrate
        assert notifier.calls == []  # no merge announcement
        assert signals[0].payload["decision"] == "blocked"
        assert len(pinger.calls) == 1
        _, key = pinger.calls[0]
        assert key == f"substrate-hold:{SLUG}#6230:{clear.reviewed_sha}"

    def test_non_substrate_uv_audit_fallback_still_raw_merges_on_keystone_refusal(self) -> None:
        # Anti-vacuity twin: the NON-substrate uv-audit fallback escalation is
        # unchanged — a logic-class CLEAR whose keystone refuses still raw-merges
        # via gh and never pings. Guards against over-gating Finding 1.
        _issue_clear()
        api = FakePrApiClient(
            prs_by_slug={SLUG: [_open_pr(checks=(_green_required(), _red_uv_audit()))]},
            main_uv_audit_red=True,
            fallback_succeeds=True,
        )
        keystone = FakeKeystone(merged=False, error="uv-audit failing", escalation_kind="")
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, substrate_pinger=pinger)

        signals = scanner.scan()

        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]  # non-substrate still escalates
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, True)]
        assert signals[0].payload["reason"] == "fallback_uv_audit_gh"
        assert pinger.calls == []

    def test_re_tick_does_not_double_ping_real_notify_dedupe(self) -> None:
        # The anti-vacuity test (d): re-running the sweep on the SAME held head
        # does not double-ping. Uses the REAL NotifyWithFallbackSubstratePinger so
        # the BotPing idempotency ledger genuinely dedupes across two ticks; only
        # the unstoppable Slack HTTP boundary is faked.
        clear = _issue_clear()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone(merged=False, error="held: substrate", escalation_kind="substrate")
        scanner, _ = _scanner(api=api, keystone=keystone, substrate_pinger=NotifyWithFallbackSubstratePinger())

        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.post_message.return_value = {"ok": True, "ts": "1700000000.000100"}
        backend.get_permalink.return_value = "https://x.slack.com/p1"
        backend.fetch_message.return_value = {"ts": "1700000000.000100", "text": "held"}

        with (
            patch("teatree.core.notify.messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify.resolve_user_id", return_value="U_ME"),
        ):
            scanner.scan()
            scanner.scan()

        key = f"substrate-hold:{SLUG}#6230:{clear.reviewed_sha}"
        assert BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).count() == 1
        # Exactly one Slack post landed despite two ticks — the ledger deduped.
        assert backend.post_message.call_count == 1


class TestSoloOverlaySubstrateHold:
    """Finding 2 (fail-open): the solo-overlay no-CLEAR bypass must hold substrate.

    The bypass raw-merges a green+clean+cold-reviewed own PR via
    ``merge_pr_squash_bound`` when no CLEAR exists — with ZERO substrate gating.
    A substrate PR on a solo overlay (cold-review, no CLEAR) would therefore
    auto-merge with no hold and no ping, bypassing the keystone substrate
    guarantee. The fix classifies the PR's changed paths before the direct
    merge; a substrate diff (or an unfetchable one — fail-safe) HOLDS + pings
    instead of merging.
    """

    def test_solo_overlay_substrate_pr_holds_and_pings_no_raw_merge(self) -> None:
        # Cold-review recorded, no CLEAR, CI green — the bypass would merge — but
        # the diff touches a substrate path, so it HOLDS + pings instead.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True, substrate_pinger=pinger)

        with patch(
            "teatree.loop.scanners.pr_sweep_substrate.fetch_pr_changed_paths",
            return_value=["src/teatree/core/merge/authorization.py"],
        ):
            signals = scanner.scan()

        assert api.merge_pr_calls == []  # the raw gh merge was NOT fired for substrate
        assert notifier.calls == []  # no merge announcement
        assert signals[0].payload["decision"] == "blocked"
        assert len(pinger.calls) == 1
        _, key = pinger.calls[0]
        assert key == f"substrate-hold:{SLUG}#6230:{HEAD}"

    def test_solo_overlay_non_substrate_pr_still_merges_via_gh_fallback(self) -> None:
        # Anti-vacuity twin: a NON-substrate diff on the solo overlay still merges
        # via the direct gh fallback. Guards against over-gating Finding 2.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True, substrate_pinger=pinger)

        with patch(
            "teatree.loop.scanners.pr_sweep_substrate.fetch_pr_changed_paths",
            return_value=["src/teatree/loop/scanners/pr_sweep.py"],
        ):
            signals = scanner.scan()

        assert api.merge_pr_calls == [(SLUG, 6230, HEAD)]  # non-substrate still merges
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, False)]
        assert pinger.calls == []
        assert signals[0].payload["reason"] == "solo_overlay_no_clear"

    def test_solo_overlay_unfetchable_paths_fail_safe_holds(self) -> None:
        # FAIL-SAFE: a real PR always changes >=1 file, so an empty changed-paths
        # list signals the forge fetch failed. The bypass must treat the can't-tell
        # case conservatively — HOLD + ping, never widen to a silent merge.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True, substrate_pinger=pinger)

        with patch("teatree.loop.scanners.pr_sweep_substrate.fetch_pr_changed_paths", return_value=[]):
            signals = scanner.scan()

        assert api.merge_pr_calls == []  # the can't-tell case held, did not merge
        assert notifier.calls == []
        assert signals[0].payload["decision"] == "blocked"
        assert len(pinger.calls) == 1

    def test_solo_overlay_paths_fetch_raises_fail_safe_holds(self) -> None:
        # FAIL-SAFE: a forge exception during the changed-paths fetch must also hold,
        # never crash the sweep into the silent-merge branch.
        _record_cold_review()
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        pinger = FakeSubstratePinger()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True, substrate_pinger=pinger)

        with patch(
            "teatree.loop.scanners.pr_sweep_substrate.fetch_pr_changed_paths",
            side_effect=RuntimeError("forge down"),
        ):
            signals = scanner.scan()

        assert api.merge_pr_calls == []
        assert notifier.calls == []
        assert signals[0].payload["decision"] == "blocked"
        assert len(pinger.calls) == 1
