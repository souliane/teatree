"""The sweep names the CLEAR it could not use, and tells a human (#4249).

An absent authorisation used to surface as ``solo_overlay_no_review`` — a definite
verdict about review — on an INTERNAL (log-only) flag, so a re-issuable CLEAR sat
behind a log line every tick with no surface naming the real cause.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models.merge_clear import ClearRequest, MergeClear
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.pr_sweep import CLEAR_PRESENT_UNUSABLE_REASON, PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import OWNER_ESCALATION_FLAG_REASONS, NullMergeNotifier, SlackMergeNotifier
from teatree.types import RawAPIDict

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
STALE = "deadbeef00000000000000000000000000000000"
MERGED_SHA = "abcdef1234567890abcdef1234567890abcdef12"
SELF_LOGIN = "souliane"

#: Everything machine-dependent the sweep reads: the registry enumeration and clone
#: origin the slug resolver walks, the live changed-paths + branch-protection reads,
#: and the #1773 visibility probe (these cases gate on the CLEAR, not on that rung).
_AMBIENT_READS: tuple[tuple[str, object], ...] = (
    ("teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs", [SLUG]),
    ("teatree.core.merge.pr_slug_resolution._project_repo_slug", SLUG),
    ("teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths", ["src/teatree/loop/scanners/pr_sweep.py"]),
    ("teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names", {"test (3.13)"}),
    ("teatree.core.review.author_trust.repo_is_internal", True),
)


@dataclass(slots=True)
class _Api:
    prs: list[PrSummary] = field(default_factory=list)
    merge_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return [pr for pr in self.prs if pr.slug == slug]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        _ = (slug, check_name)
        return False

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> tuple[bool, str]:
        self.merge_calls.append((slug, pr_id, expected_head_oid))
        return True, MERGED_SHA

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:
        _ = (slug, pr_id, expected_head_oid)
        return True


@dataclass(slots=True)
class _Keystone:
    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        _ = human_authorized
        self.calls.append(clear_id)
        return True, MERGED_SHA, "", "", ""


def _green_check() -> RawAPIDict:
    return {
        "__typename": "CheckRun",
        "name": "test (3.13)",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "startedAt": "2026-06-19T10:00:00Z",
        "completedAt": "2026-06-19T10:05:00Z",
    }


def _pr(*, pr_id: int = 6230) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=pr_id,
        head_sha=HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=(_green_check(),),
        url=f"https://github.com/{SLUG}/pull/{pr_id}",
        title=f"PR {pr_id}",
        author=SELF_LOGIN,
        same_repo=True,
    )


def _issue_clear(*, slug: str = SLUG, sha: str = HEAD, pr_id: int = 6230) -> MergeClear:
    return MergeClear.issue(
        ClearRequest(
            pr_id=pr_id,
            slug=slug,
            reviewed_sha=sha,
            reviewer_identity="cold-reviewer",
            gh_verify_result="green",
            blast_class="logic",
        )
    )


def _scanner(api: _Api, keystone: _Keystone, *, solo: bool = True) -> tuple[PrSweepScanner, NullMergeNotifier]:
    notifier = NullMergeNotifier()
    scanner = PrSweepScanner(
        repos=(SLUG,),
        api=api,
        keystone=keystone,
        notifier=notifier,
        overlay="teatree",
        solo_overlay=solo,
        self_identities=(SELF_LOGIN,),
    )
    return scanner, notifier


class TestUnusableClearIsNamed(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for target, value in _AMBIENT_READS:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_stale_clear_reports_the_clear_not_a_verdict_about_review(self) -> None:
        _issue_clear(sha=STALE)
        api = _Api(prs=[_pr()])
        scanner, notifier = _scanner(api, _Keystone())

        signals = scanner.scan()

        assert [s.payload["reason"] for s in signals] == [CLEAR_PRESENT_UNUSABLE_REASON]
        assert (
            SLUG,
            6230,
            CLEAR_PRESENT_UNUSABLE_REASON,
            f"https://github.com/{SLUG}/pull/6230",
        ) in notifier.flag_calls

    def test_no_clear_at_all_still_reports_the_missing_review(self) -> None:
        api = _Api(prs=[_pr()])
        scanner, notifier = _scanner(api, _Keystone())

        signals = scanner.scan()

        assert [s.payload["reason"] for s in signals] == ["solo_overlay_no_review"]
        assert [reason for _, _, reason, _ in notifier.flag_calls] == ["no_independent_review"]

    def test_a_stale_clear_never_blocks_a_cold_reviewed_merge(self) -> None:
        _issue_clear(sha=STALE)
        ReviewVerdict.record(
            pr_id=6230,
            slug=SLUG,
            reviewed_sha=HEAD,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        api = _Api(prs=[_pr()])
        scanner, _notifier = _scanner(api, _Keystone())

        signals = scanner.scan()

        assert api.merge_calls == [(SLUG, 6230, HEAD)]
        assert [s.payload["merged"] for s in signals] == [True]

    def test_a_branch_slugged_clear_at_the_live_head_merges_through_the_keystone(self) -> None:
        clear = _issue_clear(slug="review-fixes/docs")
        api = _Api(prs=[_pr()])
        keystone = _Keystone()
        scanner, _notifier = _scanner(api, keystone)

        signals = scanner.scan()

        assert keystone.calls == [int(clear.pk)]
        assert [s.payload["merged"] for s in signals] == [True]


class TestTheReportReachesAHuman(TestCase):
    def test_the_unusable_clear_flag_is_an_owner_audience(self) -> None:
        assert CLEAR_PRESENT_UNUSABLE_REASON in OWNER_ESCALATION_FLAG_REASONS
        with patch("teatree.core.notify.notify_user") as notify:
            SlackMergeNotifier(backend=None).flag(
                slug=SLUG, pr_id=6230, reason=CLEAR_PRESENT_UNUSABLE_REASON, url="https://example.test/pr"
            )
        assert notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION
        assert "re-issue at the current SHA" in notify.call_args.args[0]

    def test_an_ordinary_flag_stays_log_only(self) -> None:
        with patch("teatree.core.notify.notify_user") as notify:
            SlackMergeNotifier(backend=None).flag(
                slug=SLUG, pr_id=6230, reason="no_independent_review", url="https://example.test/pr"
            )
        assert notify.call_args.kwargs["audience"] is NotifyAudience.INTERNAL
