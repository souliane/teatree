import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase

from teatree.core.models.pull_request import PullRequest
from teatree.core.review.pr_inventory import PrInventoryEntry, mark_inventory_unknown, write_inventory
from teatree.dash.pr_board import build_pr_board
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.pr_sweep import MergeAttempt, PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_gitlab import GlabPrApiClient
from teatree.loop.scanners.pr_sweep_reporting import inventory_entry
from tests.factories import PullRequestFactory, TicketFactory


def _pr(number: int, *, created_at: str) -> PrSummary:
    return PrSummary(
        slug="souliane/teatree",
        number=number,
        head_sha="a" * 40,
        is_draft=False,
        has_changes_requested=False,
        url=f"https://github.com/souliane/teatree/pull/{number}",
        title=f"PR {number}",
        created_at=created_at,
    )


class ForgeInventoryTimestampTestCase(TestCase):
    def test_github_listing_requests_and_preserves_creation_and_activity_times(self) -> None:
        raw = {
            "number": 7,
            "headRefOid": "a" * 40,
            "url": "https://github.com/souliane/teatree/pull/7",
            "createdAt": "2026-10-01T10:00:00Z",
            "updatedAt": "2026-10-02T10:00:00Z",
        }
        with patch.object(GhPrApiClient, "_run_gh", return_value=(0, json.dumps([raw]), "")) as run:
            pr = GhPrApiClient().list_open_prs(slug="souliane/teatree")[0]
        fields = run.call_args_list[0].args[0]
        assert "createdAt" in fields[fields.index("--json") + 1]
        assert "updatedAt" in fields[fields.index("--json") + 1]
        assert pr.created_at == raw["createdAt"]
        assert pr.updated_at == raw["updatedAt"]

    def test_gitlab_listing_preserves_creation_and_activity_times(self) -> None:
        backend = Mock()
        backend.get_repo.return_value = {"id": 1}
        backend.list_prs.return_value = [
            {
                "iid": 8,
                "sha": "a" * 40,
                "web_url": "https://gitlab.example/souliane/teatree/-/merge_requests/8",
                "created_at": "2026-10-01T10:00:00Z",
                "updated_at": "2026-10-02T10:00:00Z",
            }
        ]
        with patch.object(GlabPrApiClient, "_backend", return_value=backend):
            pr = GlabPrApiClient().list_open_prs(slug="souliane/teatree")[0]
        assert pr.created_at == "2026-10-01T10:00:00Z"
        assert pr.updated_at == "2026-10-02T10:00:00Z"


class ForgeApprovalsTestCase(TestCase):
    def test_foreign_author_is_recorded_without_claiming_factory_ownership(self) -> None:
        pr = replace(_pr(7, created_at="2026-10-01T10:00:00Z"), author="contributor")
        entry = inventory_entry(pr, None, identities=("owner",))
        assert (entry.author, entry.owned) == ("contributor", False)

    def _listed_github_pr(self, reviews: list[dict[str, object]]) -> PrSummary:
        raw = {
            "number": 7,
            "headRefOid": "a" * 40,
            "url": "https://github.com/souliane/teatree/pull/7",
            "reviewDecision": "APPROVED",
            "reviews": reviews,
        }
        with patch.object(GhPrApiClient, "_run_gh", return_value=(0, json.dumps([raw]), "")):
            return GhPrApiClient().list_open_prs(slug="souliane/teatree")[0]

    def test_github_listing_carries_observed_approval_count_with_unknown_requirement(self) -> None:
        pr = self._listed_github_pr([{"state": "APPROVED", "author": {"login": "reviewer"}}])
        assert inventory_entry(pr, None).approvals_display == "1/unknown (approved)"

    def test_github_listing_carries_unresolved_threads_from_a_complete_forge_read(self) -> None:
        raw = {"number": 7, "headRefOid": "a" * 40, "url": "https://github.com/souliane/teatree/pull/7"}
        threads = {
            "data": {
                "repository": {
                    "p7": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"isResolved": False}, {"isResolved": True}, {"isResolved": False}],
                        }
                    }
                }
            }
        }
        with patch.object(
            GhPrApiClient,
            "_run_gh",
            side_effect=[(0, json.dumps([raw]), ""), (0, json.dumps(threads), "")],
        ):
            (pr,) = GhPrApiClient().list_open_prs(slug="souliane/teatree")
        assert inventory_entry(pr, None).threads == "2 unresolved"

    def test_failed_thread_read_does_not_claim_a_complete_count(self) -> None:
        raw = {"number": 7, "headRefOid": "a" * 40, "url": "https://github.com/souliane/teatree/pull/7"}
        threads = {
            "data": {
                "repository": {
                    "p7": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": True}]}}
                }
            }
        }
        with patch.object(
            GhPrApiClient,
            "_run_gh",
            side_effect=[(0, json.dumps([raw]), ""), (1, json.dumps(threads), "rate limited")],
        ):
            (pr,) = GhPrApiClient().list_open_prs(slug="souliane/teatree")
        assert inventory_entry(pr, None).threads == "unknown"

    def test_github_thread_read_batches_twenty_six_prs(self) -> None:
        raw = [
            {"number": number, "headRefOid": "a" * 40, "url": f"https://github.com/souliane/teatree/pull/{number}"}
            for number in range(1, 27)
        ]

        def response(numbers: range) -> str:
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            f"p{number}": {"reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
                            for number in numbers
                        }
                    }
                }
            )

        with patch.object(
            GhPrApiClient,
            "_run_gh",
            side_effect=[(0, json.dumps(raw), ""), (0, response(range(1, 26)), ""), (0, response(range(26, 27)), "")],
        ) as run:
            prs = GhPrApiClient().list_open_prs(slug="souliane/teatree")
        assert run.call_count == 3
        assert [pr.unresolved_threads for pr in prs] == [0] * 26

    def test_github_approval_count_takes_each_reviewers_standing_review_only(self) -> None:
        pr = self._listed_github_pr(
            [
                {"state": "APPROVED", "author": {"login": "ann"}},
                {"state": "COMMENTED", "author": {"login": "ann"}},
                {"state": "APPROVED", "author": {"login": "bob"}},
                {"state": "CHANGES_REQUESTED", "author": {"login": "bob"}},
                {"state": "APPROVED", "author": {"login": "cat"}},
                {"state": "APPROVED", "author": {"login": "cat"}},
            ]
        )
        assert pr.approval_count == 2

    def test_github_listing_without_a_review_list_leaves_the_approval_count_unknown(self) -> None:
        raw = {"number": 7, "headRefOid": "a" * 40, "url": "u", "reviewDecision": "REVIEW_REQUIRED"}
        with patch.object(GhPrApiClient, "_run_gh", return_value=(0, json.dumps([raw]), "")):
            pr = GhPrApiClient().list_open_prs(slug="souliane/teatree")[0]
        assert inventory_entry(pr, None).approvals_display == "awaiting approval"

    def test_github_listing_with_no_reviews_counts_zero_approvals(self) -> None:
        assert self._listed_github_pr([]).approval_count == 0

    def test_github_review_decision_is_listed_and_mapped(self) -> None:
        decisions = {"APPROVED": "approved", "REVIEW_REQUIRED": "awaiting approval", "": "unknown"}
        for decision, expected in decisions.items():
            with self.subTest(decision=decision):
                raw = {"number": 7, "headRefOid": "a" * 40, "url": "u", "reviewDecision": decision}
                with patch.object(GhPrApiClient, "_run_gh", return_value=(0, json.dumps([raw]), "")) as run:
                    pr = GhPrApiClient().list_open_prs(slug="souliane/teatree")[0]
                fields = run.call_args_list[0].args[0]
                assert "reviewDecision" in fields[fields.index("--json") + 1]
                assert pr.approvals == expected

    def test_gitlab_merge_status_is_mapped(self) -> None:
        statuses = {"mergeable": "approved", "not_approved": "awaiting approval", "ci_must_pass": "unknown"}
        for status, expected in statuses.items():
            with self.subTest(status=status):
                backend = Mock()
                backend.get_repo.return_value = {"id": 1}
                backend.list_prs.return_value = [{"iid": 8, "sha": "a" * 40, "detailed_merge_status": status}]
                with patch.object(GlabPrApiClient, "_backend", return_value=backend):
                    pr = GlabPrApiClient().list_open_prs(slug="souliane/teatree")[0]
                assert pr.approvals == expected


class PrSweepInventoryTestCase(TestCase):
    def test_partial_repo_failure_revokes_previous_ready_snapshot(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory(), state=PullRequest.State.APPROVED)
        entry = PrInventoryEntry(
            url=pr.url,
            repo=pr.repo,
            iid=int(pr.iid),
            title="Ready PR",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-02T10:00:00Z",
            next_task="Review or merge PR",
            decision="skip",
            ci="green",
            approvals="approved",
            threads="0 unresolved",
            draft=False,
        )
        api = Mock()
        api.list_open_prs.side_effect = [
            [],
            ScannerError(scanner="pr_sweep", error_class=ScannerErrorClass.UNKNOWN, detail="forge unavailable"),
        ]
        scanner = PrSweepScanner(
            repos=(pr.repo, "other/repo"),
            api=api,
            keystone=Mock(),
            notifier=Mock(),
            overlay=pr.overlay,
            inventory_writer=write_inventory,
            inventory_invalidator=mark_inventory_unknown,
        )
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"T3_DATA_DIR": str(Path(temporary))}):
            write_inventory(pr.overlay, [entry], watched_repos=(pr.repo, "other/repo"))
            assert (
                next(card for group in build_pr_board().groups for card in group.cards).group
                == "Ready to merge or merging"
            )
            with pytest.raises(ScannerError):
                scanner.scan()
            board = build_pr_board()
        assert next(card for group in board.groups for card in group.cards).group != "Ready to merge or merging"
        assert "unknown" in board.inventory_warning

    def test_complete_tick_records_all_prs_and_serves_started_then_oldest(self) -> None:
        prs = [_pr(number, created_at=f"2026-10-{26 - number:02d}T10:00:00Z") for number in range(1, 26)]
        PullRequestFactory(url=prs[0].url, iid=str(prs[0].number))
        api = Mock()
        api.list_open_prs.return_value = list(reversed(prs))
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree",),
            api=api,
            keystone=Mock(),
            notifier=Mock(),
            overlay="teatree",
            inventory_writer=writer,
        )
        evaluated: list[int] = []

        def evaluate(_scanner: PrSweepScanner, pr: PrSummary) -> MergeAttempt:
            evaluated.append(pr.number)
            return MergeAttempt(slug=pr.slug, pr_id=pr.number, decision="skip", reason="awaiting_review", url=pr.url)

        with patch.object(PrSweepScanner, "_evaluate", autospec=True, side_effect=evaluate):
            scanner.scan()

        assert evaluated == [1, *range(25, 1, -1)]
        overlay, entries = writer.call_args.args
        assert overlay == "teatree"
        assert len(entries) == 25
        assert entries[0].iid == 1
        assert entries[1].created_at == "2026-10-01T10:00:00Z"
        assert all(entry.next_task for entry in entries)

    def test_uncleared_pr_inventory_names_the_action_and_behind_branch_remedy(self) -> None:
        current = _pr(1, created_at="2026-10-01T10:00:00Z")
        behind = _pr(2, created_at="2026-10-02T10:00:00Z")
        behind = replace(behind, behind_main=True)
        api = Mock()
        api.list_open_prs.return_value = [current, behind]
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree",), api=api, keystone=Mock(), notifier=Mock(), inventory_writer=writer
        )
        refusal = MergeAttempt(slug=current.slug, pr_id=1, decision="skip", reason="no_clear_for_head", url=current.url)
        with patch.object(PrSweepScanner, "_evaluate", return_value=refusal):
            scanner.scan()

        entries = writer.call_args.args[1]
        assert entries[0].next_task == "Obtain independent review and merge authorization"
        assert entries[1].next_task == "Update PR branch against main"

    def test_empty_success_clears_the_inventory(self) -> None:
        api = Mock()
        api.list_open_prs.return_value = []
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree",), api=api, keystone=Mock(), notifier=Mock(), inventory_writer=writer
        )

        assert scanner.scan() == []
        writer.assert_called_once_with("", [], watched_repos=("souliane/teatree",))

    def test_partial_forge_failure_preserves_previous_complete_snapshot(self) -> None:
        api = Mock()
        api.list_open_prs.side_effect = [
            [_pr(1, created_at="2026-10-01T10:00:00Z")],
            ScannerError(scanner="pr_sweep", error_class=ScannerErrorClass.AUTH, detail="401"),
        ]
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree", "other/repo"),
            api=api,
            keystone=Mock(),
            notifier=Mock(),
            inventory_writer=writer,
        )
        with patch.object(
            PrSweepScanner,
            "_evaluate",
            autospec=True,
            return_value=MergeAttempt(slug="souliane/teatree", pr_id=1, decision="skip", url="url"),
        ):
            scanner.scan()

        writer.assert_not_called()


class PrSweepInventoryFactsTestCase(TestCase):
    def _entries(self, pr: PrSummary, attempt: MergeAttempt) -> list:
        api = Mock()
        api.list_open_prs.return_value = [pr]
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree",), api=api, keystone=Mock(), notifier=Mock(), inventory_writer=writer
        )
        with patch.object(PrSweepScanner, "_evaluate", return_value=attempt):
            scanner.scan()
        return writer.call_args.args[1]

    def test_ci_and_approvals_come_from_the_decision_and_the_listing(self) -> None:
        pr = replace(_pr(1, created_at="2026-10-01T10:00:00Z"), approvals="approved")
        cases = {
            "no_clear_for_head": ("green", "Obtain independent review and merge authorization"),
            "ci_pending": ("pending", "Wait for CI to finish"),
            "ci_red": ("red", "Fix failing CI"),
            "draft": ("unknown", "Finish draft and request review"),
        }
        for reason, (ci, next_task) in cases.items():
            with self.subTest(reason=reason):
                attempt = MergeAttempt(slug=pr.slug, pr_id=1, decision="skip", reason=reason, url=pr.url)
                (entry,) = self._entries(pr, attempt)
                assert (entry.ci, entry.approvals, entry.next_task) == (ci, "approved", next_task)

    def test_a_pr_the_sweep_cannot_evaluate_stays_listed_and_other_signals_survive(self) -> None:
        broken, fine = _pr(1, created_at="2026-10-01T10:00:00Z"), _pr(2, created_at="2026-10-02T10:00:00Z")
        api = Mock()
        api.list_open_prs.return_value = [broken, fine]
        writer = Mock()
        scanner = PrSweepScanner(
            repos=("souliane/teatree",), api=api, keystone=Mock(), notifier=Mock(), inventory_writer=writer
        )
        attempt = MergeAttempt(slug=fine.slug, pr_id=2, decision="skip", reason="no_clear_for_head", url=fine.url)

        def evaluate(_scanner: PrSweepScanner, pr: PrSummary) -> MergeAttempt:
            if pr.number == 1:
                message = "boom"
                raise RuntimeError(message)
            return attempt

        with patch.object(PrSweepScanner, "_evaluate", autospec=True, side_effect=evaluate):
            signals = scanner.scan()

        assert [signal.payload["pr_id"] for signal in signals] == [2]
        entries = writer.call_args.args[1]
        assert [(entry.iid, entry.reason) for entry in entries] == [(1, "evaluation_failed"), (2, "no_clear_for_head")]

    def test_a_failing_inventory_write_is_reported_without_losing_signals(self) -> None:
        pr = _pr(1, created_at="2026-10-01T10:00:00Z")
        api = Mock()
        api.list_open_prs.return_value = [pr]
        writer = Mock(side_effect=OSError("disk full"))
        scanner = PrSweepScanner(
            repos=("souliane/teatree",), api=api, keystone=Mock(), notifier=Mock(), inventory_writer=writer
        )
        attempt = MergeAttempt(slug=pr.slug, pr_id=1, decision="skip", reason="no_clear_for_head", url=pr.url)
        with patch.object(PrSweepScanner, "_evaluate", return_value=attempt):
            assert len(scanner.scan()) == 1

    def test_a_failing_inventory_invalidation_preserves_merge_signals(self) -> None:
        pr = _pr(1, created_at="2026-10-01T10:00:00Z")
        api = Mock()
        api.list_open_prs.side_effect = [
            [pr],
            ScannerError(scanner="pr_sweep", error_class=ScannerErrorClass.AUTH, detail="401"),
        ]
        scanner = PrSweepScanner(
            repos=("souliane/teatree", "other/repo"),
            api=api,
            keystone=Mock(),
            notifier=Mock(),
            inventory_invalidator=Mock(side_effect=OSError("disk full")),
        )
        attempt = MergeAttempt(slug=pr.slug, pr_id=1, decision="merged", url=pr.url)

        with patch.object(PrSweepScanner, "_evaluate", return_value=attempt):
            signals = scanner.scan()

        assert [signal.kind for signal in signals] == ["pr_sweep.merged"]


class MyPrsInventoryTestCase(TestCase):
    def _host(self, *, status: str = "success") -> Mock:
        host = Mock()
        host.current_user.return_value = "owner"
        host.list_my_prs.return_value = [
            {
                "web_url": "https://github.com/souliane/teatree/pull/1",
                "iid": 1,
                "title": "Owned",
                "head_pipeline": {"status": status},
            }
        ]
        return host

    def test_green_pr_records_the_forge_approvals_and_threads(self) -> None:
        host = self._host()
        host.get_mr_approvals.return_value = {
            "approvals_left": 0,
            "approved_by": ["a"],
            "count": 1,
            "required": 1,
            "unresolved_resolvable": 2,
        }
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        host.get_mr_approvals.assert_called_once_with(repo="souliane/teatree", pr_iid=1)
        assert (entry.ci, entry.approvals, entry.threads) == ("green", "approved", "2 unresolved")
        assert entry.approvals_display == "1/1 (approved)"

    def test_skipped_pipeline_is_settled_and_reads_approvals(self) -> None:
        host = self._host(status="skipped")
        host.get_mr_approvals.return_value = {
            "approvals_left": 0,
            "approved_by": [],
            "unresolved_resolvable": 0,
        }
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        assert (entry.ci, entry.approvals, entry.threads) == ("green", "approved", "0 unresolved")
        host.get_mr_approvals.assert_called_once()

    def test_approvals_without_a_count_show_the_label_alone(self) -> None:
        host = self._host()
        host.get_mr_approvals.return_value = {"approvals_left": 1, "approved_by": [], "unresolved_resolvable": 0}
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        assert entry.approvals_display == "awaiting approval"

    def test_unreadable_approvals_are_recorded_as_unknown(self) -> None:
        host = self._host()
        host.get_mr_approvals.side_effect = RuntimeError("forge down")
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        assert (entry.approvals, entry.threads) == ("unknown", "unknown")

    def test_unparsable_pr_ref_does_not_claim_approval(self) -> None:
        host = self._host()
        host.list_my_prs.return_value[0]["web_url"] = "https://example.test/not-a-pr"
        writer = Mock()
        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()
        (entry,) = writer.call_args.args[1]
        assert (entry.approvals, entry.threads) == ("unknown", "unknown")
        host.get_mr_approvals.assert_not_called()

    def test_red_pr_does_not_spend_an_approvals_read(self) -> None:
        host = self._host(status="failed")
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        host.get_mr_approvals.assert_not_called()
        (entry,) = writer.call_args.args[1]
        assert (entry.ci, entry.next_task) == ("red", "Fix failing CI")

    def test_a_failing_inventory_write_keeps_the_scanners_signals(self) -> None:
        host = self._host(status="failed")
        writer = Mock(side_effect=OSError("disk full"))

        signals = MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        assert [signal.kind for signal in signals] == ["my_pr.failed"]

    def test_all_owned_open_prs_are_written_with_age_source_and_next_task(self) -> None:
        host = Mock()
        host.current_user.return_value = "owner"
        host.list_my_prs.return_value = [
            {
                "web_url": f"https://github.com/souliane/teatree/pull/{number}",
                "iid": number,
                "title": f"Owned PR {number}",
                "created_at": "2026-10-01T10:00:00Z",
                "updated_at": "2026-10-02T10:00:00Z",
                "head_pipeline": {"status": "success"},
            }
            for number in range(1, 26)
        ]
        writer = Mock()
        scanner = MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer)

        scanner.scan()

        key, entries = writer.call_args.args
        assert key.startswith("own:teatree:")
        assert len(entries) == 25
        assert entries[0].repo == "souliane/teatree"
        assert entries[0].created_at == "2026-10-01T10:00:00Z"
        assert entries[0].ci == "green"
        assert all(entry.next_task for entry in entries)

    def test_partial_identity_read_preserves_previous_complete_inventory(self) -> None:
        host = Mock()
        host.list_my_prs.side_effect = [
            [{"web_url": "https://github.com/souliane/teatree/pull/1", "iid": 1, "title": "First"}],
            RuntimeError("forge unavailable"),
        ]
        writer = Mock()
        scanner = MyPrsScanner(host=host, identities=("one", "two"), overlay_name="teatree", inventory_writer=writer)

        signals = scanner.scan()

        assert len(signals) == 1
        writer.assert_not_called()

    def test_a_failing_inventory_invalidation_preserves_owned_pr_signals(self) -> None:
        host = self._host(status="failed")
        host.list_my_prs.side_effect = [host.list_my_prs.return_value, RuntimeError("forge unavailable")]
        scanner = MyPrsScanner(
            host=host,
            identities=("one", "two"),
            overlay_name="teatree",
            inventory_invalidator=Mock(side_effect=OSError("disk full")),
        )

        signals = scanner.scan()

        assert [signal.kind for signal in signals] == ["my_pr.failed"]

    def test_same_class_hosts_with_empty_prefixes_keep_distinct_inventory_keys(self) -> None:
        keys = []
        for origin in ("https://gitlab-one.example", "https://gitlab-two.example"):
            writer = Mock()
            MyPrsScanner(
                host=self._host(),
                overlay_name="teatree",
                host_origin=origin,
                inventory_writer=writer,
            ).scan()
            keys.append(writer.call_args.args[0])

        assert keys[0] != keys[1]
        assert all(key.startswith("own:teatree:") for key in keys)

    def test_failed_next_list_read_revokes_verified_own_inventory(self) -> None:
        host = self._host()
        host.get_mr_approvals.return_value = {"approvals_left": 0, "unresolved_resolvable": 0}
        host.list_my_prs.side_effect = [host.list_my_prs.return_value, RuntimeError("forge unavailable")]
        url = host.list_my_prs.return_value[0]["web_url"]
        PullRequestFactory(ticket=TicketFactory(), url=url, state=PullRequest.State.APPROVED)
        scanner = MyPrsScanner(
            host=host,
            identities=("owner",),
            overlay_name="",
            inventory_writer=write_inventory,
            inventory_invalidator=mark_inventory_unknown,
        )

        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"T3_DATA_DIR": str(Path(temporary))}):
            scanner.scan()
            assert next(card for group in build_pr_board().groups for card in group.cards).group == "Blocked"
            scanner.scan()
            board = build_pr_board()

        card = next(card for group in board.groups for card in group.cards)
        assert card.group != "Ready to merge or merging"
        assert "unknown" in board.inventory_warning
