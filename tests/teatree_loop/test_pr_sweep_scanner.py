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

from dataclasses import dataclass, field

import pytest

from teatree.core.models.merge_clear import ClearRequest, MergeClear
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.pr_sweep import CheckResult, NullMergeNotifier, PrSummary, PrSweepScanner

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


def _scanner(
    *,
    api: FakePrApiClient,
    keystone: FakeKeystone,
    notifier: NullMergeNotifier | None = None,
    repos: tuple[str, ...] = (SLUG,),
    solo_overlay: bool = False,
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

    def test_solo_overlay_with_no_clear_merges_via_gh_fallback(self) -> None:
        # No CLEAR issued for this PR — the dogfood case.
        api = FakePrApiClient(prs_by_slug={SLUG: [_open_pr()]})
        keystone = FakeKeystone()
        scanner, notifier = _scanner(api=api, keystone=keystone, solo_overlay=True)

        signals = scanner.scan()

        assert keystone.calls == []  # CLEAR-keystone never invoked
        assert api.merge_pr_calls == [(SLUG, 6230)]  # direct gh fallback fired
        assert notifier.calls == [(SLUG, 6230, MAIN_SHA, False)]
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
