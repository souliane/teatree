"""The merge-conflict sweep: every open merge request owes a resolved conflict.

The rule is unconditional, so what these cases mostly pin is what the scanner
must NOT filter out — a draft and a review-exempt repo are both fully covered,
and each is a filter a sibling surface legitimately applies, so each is a way the
sweep could quietly stop covering most of its subject.

The other half is the three-valued read. An unanswered merge state is neither a
conflict nor a clean merge, and the scanner is pinned to report it as neither —
with a green control alongside proving the fake CAN produce a conflict, so the
silence is the scanner's decision and not a broken harness.
"""

from django.test import TestCase

from teatree.core.backend_protocols import MergeConflictState, PrMergeState
from teatree.core.models import ConfigSetting, RedMrFixAttempt, Task
from teatree.core.review.repo_exemption import mr_url_is_review_exempt, review_exempt_patterns
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanner_factories import _mr_conflict_scanner_for
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.mr_conflict import MrConflictScanner
from teatree.types import RawAPIDict
from tests.teatree_loop.test_scanners import FakeCodeHost

_SLUG = "org/repo"
_EXEMPT_SLUG = "devops/charts"


def _mr(pr_id: int, *, slug: str = _SLUG, sha: str = "", **payload: object) -> RawAPIDict:
    return {
        "number": pr_id,
        "title": f"MR {pr_id}",
        "html_url": f"https://github.com/{slug}/pull/{pr_id}",
        "head": {"sha": sha or f"{pr_id:040x}"},
        **payload,
    }


def _conflicted() -> PrMergeState:
    return PrMergeState(state="OPEN", merge_commit_oid="", conflict=MergeConflictState.CONFLICTED)


def _clean() -> PrMergeState:
    return PrMergeState(state="OPEN", merge_commit_oid="", conflict=MergeConflictState.CLEAN)


def _unknown() -> PrMergeState:
    return PrMergeState(state="OPEN", merge_commit_oid="", conflict=MergeConflictState.UNKNOWN)


def _kinds(signals: list[ScanSignal]) -> list[str]:
    return [signal.kind for signal in signals]


class TestItCoversEveryOpenMergeRequest(TestCase):
    """No review-policy filter narrows the walk — the rule applies to all of them."""

    def test_a_conflicted_draft_is_reported(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[_mr(1, draft=True, isDraft=True)],
            merge_state_by_pr={(_SLUG, 1): _conflicted()},
        )

        signals = MrConflictScanner(host=host).scan()

        assert _kinds(signals) == ["my_pr.conflicted"]

    def test_a_conflicted_merge_request_in_a_review_exempt_repo_is_reported(self) -> None:
        ConfigSetting.objects.set_value("review_exempt_repos", [_EXEMPT_SLUG])
        url = f"https://github.com/{_EXEMPT_SLUG}/pull/2"
        # The exemption must be real for the case to mean anything: if the repo were
        # not actually exempt, the scanner emitting would prove nothing.
        assert mr_url_is_review_exempt(url), review_exempt_patterns()
        host = FakeCodeHost(
            user="alice",
            my_prs=[_mr(2, slug=_EXEMPT_SLUG)],
            merge_state_by_pr={(_EXEMPT_SLUG, 2): _conflicted()},
        )

        signals = MrConflictScanner(host=host).scan()

        assert _kinds(signals) == ["my_pr.conflicted"]

    def test_a_clean_merge_request_is_silent(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(3)], merge_state_by_pr={(_SLUG, 3): _clean()})

        assert MrConflictScanner(host=host).scan() == []


class TestAnUnreadMergeStateIsNeitherConflictedNorClean(TestCase):
    """The third value is emitted as itself — never as a conflict, never as silence."""

    def test_an_unknown_merge_state_does_not_report_a_conflict(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(4)], merge_state_by_pr={(_SLUG, 4): _unknown()})

        signals = MrConflictScanner(host=host).scan()

        assert _kinds(signals) == ["my_pr.conflict_unknown"]

    def test_the_same_fake_does_report_a_conflict_when_the_forge_says_so(self) -> None:
        """The green control for the case above — the harness CAN emit a conflict."""
        host = FakeCodeHost(user="alice", my_prs=[_mr(4)], merge_state_by_pr={(_SLUG, 4): _conflicted()})

        signals = MrConflictScanner(host=host).scan()

        assert _kinds(signals) == ["my_pr.conflicted"]

    def test_a_raising_probe_is_unknown_not_clean(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(5)], raise_on_merge_state=RuntimeError("forge down"))

        assert _kinds(MrConflictScanner(host=host).scan()) == ["my_pr.conflict_unknown"]

    def test_an_unreadable_merge_state_dispatches_no_fix(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(6)], merge_state_by_pr={(_SLUG, 6): _unknown()})

        actions = dispatch(MrConflictScanner(host=host).scan())

        assert [action.kind for action in actions] == ["statusline"]


class TestOneFixPerHead(TestCase):
    """Re-ticking a still-conflicted merge request must not re-dispatch its fix."""

    @staticmethod
    def _dispatch_and_persist(host: FakeCodeHost) -> list[Task]:
        return persist_agent_actions(dispatch(MrConflictScanner(host=host).scan()))

    def test_two_ticks_on_the_same_head_dispatch_once(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(7)], merge_state_by_pr={(_SLUG, 7): _conflicted()})

        first = self._dispatch_and_persist(host)
        # The first task must be closed out, or the open-task guard — not the ledger —
        # would be what suppresses the second dispatch and the dedupe would be untested.
        Task.objects.update(status=Task.Status.COMPLETED)
        second = self._dispatch_and_persist(host)

        assert len(first) == 1
        assert second == []
        assert RedMrFixAttempt.objects.filter(kind=RedMrFixAttempt.Kind.MERGE_CONFLICT).count() == 1

    def test_a_new_head_after_a_push_dispatches_again(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(8)], merge_state_by_pr={(_SLUG, 8): _conflicted()})
        assert len(self._dispatch_and_persist(host)) == 1
        Task.objects.update(status=Task.Status.COMPLETED)

        host.my_prs = [_mr(8, sha="f" * 40)]
        second = self._dispatch_and_persist(host)

        assert len(second) == 1
        assert RedMrFixAttempt.objects.filter(kind=RedMrFixAttempt.Kind.MERGE_CONFLICT).count() == 2

    def test_the_scheduled_fix_merges_the_target_branch_rather_than_rebasing(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(9)], merge_state_by_pr={(_SLUG, 9): _conflicted()})

        task = self._dispatch_and_persist(host)[0]

        assert "MERGING the target branch" in task.execution_reason
        assert "never by rebasing" in task.execution_reason

    def test_a_conflict_does_not_consume_the_ci_fix_slot_at_the_same_head(self) -> None:
        """The two conditions are independent, so their ledger slots must be too."""
        url = f"https://github.com/{_SLUG}/pull/10"
        sha = f"{10:040x}"
        assert RedMrFixAttempt.claim(pr_url=url, head_sha=sha, kind=RedMrFixAttempt.Kind.CI_RED) is not None
        host = FakeCodeHost(user="alice", my_prs=[_mr(10)], merge_state_by_pr={(_SLUG, 10): _conflicted()})

        assert len(self._dispatch_and_persist(host)) == 1


class TestItShipsInert(TestCase):
    """Default-OFF means no scanner is built at all, not a scanner that stays quiet."""

    @staticmethod
    def _backend() -> object:
        from teatree.core.backend_factory import OverlayBackends  # noqa: PLC0415 — test-local wiring seam

        return OverlayBackends(name="t3-teatree", hosts=(FakeCodeHost(user="alice"),), identities=("alice",))

    def test_no_scanner_is_built_by_default(self) -> None:
        backend = self._backend()

        assert _mr_conflict_scanner_for(backend, backend.hosts[0]) is None

    def test_a_scanner_is_built_once_the_overlay_opts_in(self) -> None:
        ConfigSetting.objects.set_value("mr_conflict_scan_enabled", value=True)
        backend = self._backend()

        assert _mr_conflict_scanner_for(backend, backend.hosts[0]) is not None
