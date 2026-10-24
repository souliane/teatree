import json
from dataclasses import replace
from unittest.mock import Mock, patch

from django.test import TestCase

from teatree.loop.scanners.base import ScannerError, ScannerErrorClass
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.pr_sweep import MergeAttempt, PrSummary, PrSweepScanner
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_gitlab import GlabPrApiClient
from tests.factories import PullRequestFactory


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
        fields = run.call_args.args[0]
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
    def test_github_review_decision_is_listed_and_mapped(self) -> None:
        decisions = {"APPROVED": "approved", "REVIEW_REQUIRED": "awaiting approval", "": "unknown"}
        for decision, expected in decisions.items():
            with self.subTest(decision=decision):
                raw = {"number": 7, "headRefOid": "a" * 40, "url": "u", "reviewDecision": decision}
                with patch.object(GhPrApiClient, "_run_gh", return_value=(0, json.dumps([raw]), "")) as run:
                    pr = GhPrApiClient().list_open_prs(slug="souliane/teatree")[0]
                fields = run.call_args.args[0]
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
        writer.assert_called_once_with("", [])

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
        host.get_mr_approvals.return_value = {"approvals_left": 0, "approved_by": ["a"], "unresolved_resolvable": 2}
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        host.get_mr_approvals.assert_called_once_with(repo="souliane/teatree", pr_iid=1)
        assert (entry.ci, entry.approvals, entry.threads) == ("green", "approved", "2 unresolved")

    def test_unreadable_approvals_are_recorded_as_unknown(self) -> None:
        host = self._host()
        host.get_mr_approvals.side_effect = RuntimeError("forge down")
        writer = Mock()

        MyPrsScanner(host=host, overlay_name="teatree", inventory_writer=writer).scan()

        (entry,) = writer.call_args.args[1]
        assert (entry.approvals, entry.threads) == ("unknown", "unknown")

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
