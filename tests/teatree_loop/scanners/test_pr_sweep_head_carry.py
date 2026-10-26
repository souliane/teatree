"""The sweep merges on a clearance that followed an unchanged patch to a new head, and updates a patch once (#5450)."""

from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase

from teatree.core.merge.head_carry_over import PatchLineage
from teatree.core.models import BranchUpdateAttempt, HeadCarryOver, MergeClear, ReviewVerdict
from teatree.loop.scanners.pr_sweep import CLEAR_PRESENT_UNUSABLE_REASON, PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import NullMergeNotifier
from teatree.loop.scanners.pr_sweep_types import BoundMergeResult
from teatree.types import RawAPIDict
from tests._pr_ledger import own_pr

SLUG = "souliane/teatree"
PR_ID = 6230
REVIEWED = "feedfacecafebabe1234567890abcdef12345678"
LIVE = "0123456789abcdef0123456789abcdef01234567"
MERGED_SHA = "abcdef1234567890abcdef1234567890abcdef12"
SELF_LOGIN = "souliane"

_AMBIENT_READS: tuple[tuple[str, object], ...] = (
    ("teatree.core.merge.pr_slug_resolution._iter_candidate_repo_slugs", [SLUG]),
    ("teatree.core.merge.pr_slug_resolution._project_repo_slug", SLUG),
    ("teatree.core.merge.ci_rollup.CodeHostQuery.pr_changed_paths", ["src/teatree/loop/scanners/pr_sweep.py"]),
    ("teatree.core.merge.ci_rollup.CodeHostQuery.required_context_names", {"test (3.13)"}),
    ("teatree.core.review.author_trust.repo_is_internal", True),
)


def _check(conclusion: str) -> RawAPIDict:
    return {
        "__typename": "CheckRun",
        "name": "test (3.13)",
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": "2026-06-19T10:00:00Z",
        "completedAt": "2026-06-19T10:05:00Z",
    }


def _pr(*, conclusion: str = "SUCCESS", behind_main: bool = False, head: str = LIVE) -> PrSummary:
    own_pr(SLUG, PR_ID)
    return PrSummary(
        slug=SLUG,
        number=PR_ID,
        head_sha=head,
        is_draft=False,
        has_changes_requested=False,
        rollup=(_check(conclusion),),
        url=f"https://github.com/{SLUG}/pull/{PR_ID}",
        title=f"PR {PR_ID}",
        behind_main=behind_main,
        author=SELF_LOGIN,
        same_repo=True,
    )


@dataclass(slots=True)
class _Api:
    prs: list[PrSummary] = field(default_factory=list)
    update_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def list_open_prs(self, *, slug: str) -> list[PrSummary]:
        return [pr for pr in self.prs if pr.slug == slug]

    def main_check_failed(self, *, slug: str, check_name: str) -> bool:
        _ = (slug, check_name)
        return False

    def merge_pr_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> BoundMergeResult:
        _ = (slug, pr_id, expected_head_oid)
        return BoundMergeResult(merged=True, merged_sha=MERGED_SHA)

    def update_pr_branch(self, *, slug: str, pr_id: int, expected_head_oid: str) -> bool:
        self.update_calls.append((slug, pr_id, expected_head_oid))
        return True


@dataclass(slots=True)
class _Keystone:
    calls: list[int] = field(default_factory=list)

    def merge_clear(self, *, clear_id: int, human_authorized: str = "") -> tuple[bool, str, str, str, str]:
        _ = human_authorized
        self.calls.append(clear_id)
        return True, MERGED_SHA, "", "", ""


@dataclass(slots=True)
class _Lineage:
    answer: PatchLineage
    calls: int = 0

    def __call__(self, *, slug: str, pr_id: int, host_kind: str, old_head: str, new_head: str) -> PatchLineage:
        _ = (slug, pr_id, host_kind, old_head, new_head)
        self.calls += 1
        return self.answer


@dataclass(slots=True)
class _Review:
    dispatched: list[tuple[str, int, str, str, str]] = field(default_factory=list)

    def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
        self.dispatched.append((slug, pr_id, head_sha, pr_url, overlay))
        return True


def _identical() -> PatchLineage:
    return PatchLineage(old_patch_id="p" * 40, new_patch_id="p" * 40, descends=True, added_all_merges=True)


def _differing() -> PatchLineage:
    return PatchLineage(old_patch_id="p" * 40, new_patch_id="q" * 40, descends=True, added_all_merges=True)


def _reviewed_and_cleared_at_the_old_head() -> MergeClear:
    ReviewVerdict.record(
        pr_id=PR_ID, slug=SLUG, reviewed_sha=REVIEWED, verdict="merge_safe", reviewer_identity="cold-reviewer"
    )
    return MergeClear.objects.create(
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=REVIEWED,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


def _scanner(api: _Api, keystone: _Keystone, lineage: _Lineage, *, review: _Review | None = None) -> PrSweepScanner:
    return PrSweepScanner(
        repos=(SLUG,),
        api=api,
        keystone=keystone,
        notifier=NullMergeNotifier(),
        overlay="teatree",
        solo_overlay=review is not None,
        auto_review_dispatch=review is not None,
        review_dispatcher=review,
        self_identities=(SELF_LOGIN,),
        head_lineage=lineage,
    )


class _SweepCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for target, value in _AMBIENT_READS:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestACarriedClearanceMerges(_SweepCase):
    def test_a_clearance_that_followed_an_unchanged_patch_merges_with_no_review_dispatched(self) -> None:
        clear = _reviewed_and_cleared_at_the_old_head()
        review = _Review()
        keystone = _Keystone()
        scanner = _scanner(_Api(prs=[_pr()]), keystone, _Lineage(_identical()), review=review)

        signals = scanner.scan()

        assert keystone.calls == [clear.pk]
        assert review.dispatched == []
        assert [signal.payload["merged"] for signal in signals] == [True]
        clear.refresh_from_db()
        assert clear.reviewed_sha == LIVE

    def test_required_ci_still_gates_the_new_head(self) -> None:
        clear = _reviewed_and_cleared_at_the_old_head()
        keystone = _Keystone()
        scanner = _scanner(_Api(prs=[_pr(conclusion="FAILURE")]), keystone, _Lineage(_identical()))

        signals = scanner.scan()

        assert keystone.calls == []
        assert [signal.payload["reason"] for signal in signals] == ["ci_red"]
        clear.refresh_from_db()
        assert clear.reviewed_sha == LIVE

    def test_a_differing_patch_leaves_the_clearance_unusable_as_before(self) -> None:
        clear = _reviewed_and_cleared_at_the_old_head()
        keystone = _Keystone()
        review = _Review()
        scanner = _scanner(_Api(prs=[_pr()]), keystone, _Lineage(_differing()), review=review)

        signals = scanner.scan()

        assert keystone.calls == []
        assert [signal.payload["reason"] for signal in signals] == [CLEAR_PRESENT_UNUSABLE_REASON]
        assert len(review.dispatched) == 1
        clear.refresh_from_db()
        assert clear.reviewed_sha == REVIEWED

    def test_a_head_decided_once_is_never_read_from_the_forge_again(self) -> None:
        _reviewed_and_cleared_at_the_old_head()
        lineage = _Lineage(_differing())
        scanner = _scanner(_Api(prs=[_pr()]), _Keystone(), lineage)

        scanner.scan()
        scanner.scan()

        assert lineage.calls == 1


class TestAPatchIsUpdatedOnce(_SweepCase):
    def _updated_before(self, *, patch_after: PatchLineage) -> PrSweepScanner:
        _reviewed_and_cleared_at_the_old_head()
        BranchUpdateAttempt.claim(slug=SLUG, pr_id=PR_ID, head_sha=REVIEWED)
        self.api = _Api(prs=[_pr(conclusion="FAILURE", behind_main=True)])
        return _scanner(self.api, _Keystone(), _Lineage(patch_after))

    def test_a_red_behind_pr_whose_head_came_from_a_sweep_update_of_the_same_patch_is_not_updated_again(self) -> None:
        scanner = self._updated_before(patch_after=_identical())

        signals = scanner.scan()

        assert self.api.update_calls == []
        assert [signal.payload["decision"] for signal in signals] == ["needs_branch_update"]
        assert HeadCarryOver.objects.get(to_head=LIVE).outcome == HeadCarryOver.Outcome.CARRIED

    def test_a_fix_push_that_changes_the_patch_makes_the_update_available_again(self) -> None:
        scanner = self._updated_before(patch_after=_differing())

        signals = scanner.scan()

        assert self.api.update_calls == [(SLUG, PR_ID, LIVE)]
        assert [signal.payload["decision"] for signal in signals] == ["branch_updated"]
