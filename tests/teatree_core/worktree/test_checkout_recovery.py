from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import runner
from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.checkout_fence import CheckoutFenceError, claim_task_checkout_or_park, verify_task_checkout
from teatree.core.worktree.checkout_recovery import attest_deferred_exit
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.occupancy import acquire, release, task_holder_id
from teatree.mcp.serializers import serialize_worktree
from teatree.utils.git import run_strict
from tests._process_table_venue import nested_venue, table_with_unreadable_pid
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class InterruptedRun(BaseException):
    pass


class _RecoveryCase(TestCase):
    next_phase = "testing"

    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.path, branch="feature", commits_ahead=0)
        self.ticket = TicketFactory()
        self.previous = TaskFactory(ticket=self.ticket, phase="coding")
        self.next_task = TaskFactory(ticket=self.ticket, phase=self.next_phase)
        self.worktree = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path)})
        self.clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear):
            verify_task_checkout(self.previous, str(self.path))
        self.anchor = checkout_state(self.path).digest
        acquire(self.worktree, holder=task_holder_id(self.previous))
        self.interrupt()

    def interrupt(self) -> None:
        self.commit_change()
        release(self.worktree, holder=task_holder_id(self.previous))

    def commit_change(self) -> None:
        (self.path / "change.txt").write_text("interrupted change")
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["add", "change.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "interrupted change"])

    def dispatch(self, table: ProcessTable | None = None) -> None:
        acquire(self.worktree, holder=task_holder_id(self.next_task))
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table or self.clear):
            verify_task_checkout(self.next_task, str(self.path))

    def assert_parked(self, table: ProcessTable | None = None) -> None:
        with pytest.raises(CheckoutFenceError):
            self.dispatch(table)
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor


class CheckoutRecoveryTests(_RecoveryCase):
    def test_namespace_less_previous_writer_still_parks_for_host_gap(self) -> None:
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table):
            assert claim_task_checkout_or_park(self.next_task) is not None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_mixed_previous_tenures_still_park_for_host_gap(self) -> None:
        extra = self.worktree.get_extra()
        prior = extra["checkout_custody"]["tenures"][0]
        extra["checkout_custody"]["tenures"] = [prior, {**prior, "namespace": "pid:[42]"}]
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table):
            assert claim_task_checkout_or_park(self.next_task) is not None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_nested_previous_writer_sets_aside_only_host_namespace_gap(self) -> None:
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = "pid:[42]"
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        gap = "pid 21 (claude) is unreadable by this uid"
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        assert table.eviction_refuse_reason() == gap

        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table):
            assert claim_task_checkout_or_park(self.next_task) is None

        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path).digest
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["process_table"]["set_aside"] == [gap]
        assert audit["tenures"][0]["namespace"] == "pid:[42]"

    def test_nested_previous_writer_still_refuses_nested_unreadable_pid(self) -> None:
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = "pid:[42]"
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\t7\n")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table):
            assert claim_task_checkout_or_park(self.next_task) is not None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_nested_previous_writer_still_refuses_visible_checkout_process(self) -> None:
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = "pid:[42]"
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        self.assert_parked(ProcessTable(frozenset({self.path}), "/host-proc"))

    def test_a_hand_holder_between_runs_refuses(self) -> None:
        acquire(self.worktree, holder="operator", holder_session="terminal")
        release(self.worktree, holder="operator", holder_session="terminal")
        self.assert_parked()

    def test_unusable_process_table_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset(), "", ("unreadable",)))

    def test_unreadable_same_user_process_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset(), "/proc", (), ("pid 7 (python) is unreadable by this uid",)))

    def test_live_process_refuses_then_a_retry_recovers(self) -> None:
        self.assert_parked(ProcessTable(frozenset({self.path}), "/proc"))
        release(self.worktree, holder=task_holder_id(self.next_task))
        self.dispatch()
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path).digest

    def test_wrong_dispatch_branch_refuses(self) -> None:
        run_strict(repo=str(self.path), args=["checkout", "-b", "other"])
        self.assert_parked()

    def test_wrong_sibling_branch_refuses(self) -> None:
        sibling = self.path.parent / "sibling"
        _init_repo_with_branch(sibling, branch="feature", commits_ahead=0)
        WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(sibling)})
        run_strict(repo=str(sibling), args=["checkout", "-b", "other"])
        self.assert_parked()

    def test_unreadable_sibling_refuses(self) -> None:
        WorktreeFactory(
            ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path.parent / "missing")}
        )
        self.assert_parked()

    def test_empty_expected_branch_refuses(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="")
        self.assert_parked()

    def test_state_changed_during_recovery_refuses(self) -> None:
        stale = CheckoutState(digest="different", commits_ahead=1, uncommitted=0, branch="feature")
        with patch("teatree.core.worktree.checkout_recovery.checkout_state", return_value=stale):
            self.assert_parked()

    def test_unpersisted_attestation_refuses(self) -> None:
        with patch.object(Worktree, "merge_extra", return_value=None):
            self.assert_parked()

    def test_read_only_previous_holder_refuses(self) -> None:
        Task.objects.filter(pk=self.previous.pk).update(phase="reviewing")
        self.assert_parked()

    def test_legacy_attestation_without_custody_refuses(self) -> None:
        raw = dict(self.worktree.extra)
        raw.pop("checkout_custody")
        Worktree.objects.filter(pk=self.worktree.pk).update(extra=raw)
        self.assert_parked()

    def test_cancelled_holder_refuses(self) -> None:
        Task.objects.filter(pk=self.previous.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        self.assert_parked()

    def test_audit_is_exposed_without_a_page(self) -> None:
        with (
            patch("teatree.core.worktree.checkout_fence.notify_user") as page,
            patch.object(forge_pr_probe, "find_open_pr_for_branch") as probe,
        ):
            self.dispatch()
        page.assert_not_called()
        probe.assert_not_called()
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["previous"] == self.anchor
        assert audit["digest"] == checkout_state(self.path).digest
        assert audit["tenures"][0]["holder"] == task_holder_id(self.previous)
        assert audit["process_table"] == {"source": "/proc", "gaps": []}
        assert audit["branches"] == {str(self.path): "feature"}
        assert audit["pull_requests"] == {}
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit


class ReRootedCheckoutRecoveryTests(_RecoveryCase):
    next_phase = "shipping"

    def setUp(self) -> None:
        super().setUp()
        self.pr = PullRequestFactory(ticket=self.ticket)

    def interrupt(self) -> None:
        run_strict(repo=str(self.path), args=["checkout", "-b", "rerooted"])
        self.commit_change()
        Worktree.objects.filter(pk=self.worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))

    def claim(self) -> TaskAttempt | None:
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear),
            patch("teatree.core.worktree.checkout_fence.notify_user"),
        ):
            return claim_task_checkout_or_park(self.next_task)

    def assert_claim_parked(self) -> None:
        attempt = self.claim()
        assert attempt is not None
        assert "checkout_parked:" in attempt.result["summary"]
        assert "differs from its attested state" in attempt.result["summary"]
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.anchor

    def test_a_checkout_on_its_pull_requests_head_branch_is_claimed(self) -> None:
        with patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found(self.pr.url)) as probe:
            assert self.claim() is None
        probe.assert_called_once_with(str(self.path), "rerooted")
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path).digest
        assert self.worktree.branch == "feature"
        assert audit["branches"] == {str(self.path): "rerooted"}
        assert audit["pull_requests"] == {str(self.path): self.pr.url}
        assert audit["tenures"][0]["ended"] == "abandoned"

    def test_a_sibling_on_its_pull_requests_head_branch_is_claimed(self) -> None:
        sibling = self.path.parent / "sibling"
        _init_repo_with_branch(sibling, branch="feature", commits_ahead=0)
        WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(sibling)})
        run_strict(repo=str(sibling), args=["checkout", "-b", "rerooted"])
        with patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found(self.pr.url)) as probe:
            assert self.claim() is None
        probe.assert_any_call(str(self.path), "rerooted")
        probe.assert_any_call(str(sibling), "rerooted")
        assert probe.call_count == 2
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert audit["pull_requests"] == {str(self.path): self.pr.url, str(sibling): self.pr.url}

    def test_a_branch_the_ticket_does_not_own_stays_parked(self) -> None:
        foreign = PullRequestFactory(ticket=TicketFactory())
        for answer in (PrProbe.none(), PrProbe.unknown(), PrProbe.found(foreign.url)):
            with (
                self.subTest(answer=answer),
                patch.object(forge_pr_probe, "find_open_pr_for_branch", return_value=answer) as probe,
            ):
                self.assert_claim_parked()
                probe.assert_called_once_with(str(self.path), "rerooted")

    def test_unparseable_found_url_stays_parked(self) -> None:
        with patch.object(
            forge_pr_probe, "find_open_pr_for_branch", return_value=PrProbe.found("https://example.com/not-a-pr")
        ) as probe:
            self.assert_claim_parked()
        probe.assert_called_once_with(str(self.path), "rerooted")


class InterruptedRunnerRecoveryTests(TestCase):
    def test_runner_records_nested_venue_and_next_claim_reattests(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "checkout"
        _init_repo_with_branch(path, branch="feature", commits_ahead=0)
        ticket = TicketFactory()
        previous = TaskFactory(ticket=ticket, phase="coding")
        next_task = TaskFactory(ticket=ticket, phase="testing")
        worktree = WorktreeFactory(ticket=ticket, branch="feature", extra={"worktree_path": str(path)})
        clear = ProcessTable(frozenset(), "/host-proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(previous, str(path))
        anchor = checkout_state(path).digest
        previous.claim(claimed_by="worker", claimed_by_session="run-1")

        def commit_change(*_args: object, **_kwargs: object) -> TaskAttempt:
            (path / "change.txt").write_text("interrupted change")
            run_strict(repo=str(path), args=["config", "user.email", "test@example.com"])
            run_strict(repo=str(path), args=["config", "user.name", "Test"])
            run_strict(repo=str(path), args=["add", "change.txt"])
            run_strict(repo=str(path), args=["commit", "-m", "interrupted change"])
            return Mock(spec=TaskAttempt)

        with (
            nested_venue(Path(temporary.name) / "proc"),
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            patch("teatree.core.worktree.occupancy._finish_task_checkout", side_effect=InterruptedRun),
            patch.object(runner, "_run_agent", side_effect=commit_change),
            pytest.raises(InterruptedRun),
        ):
            runner.run_agent(previous, phase="coding", overlay_skill_metadata={})

        worktree.refresh_from_db()
        extra = worktree.get_extra()
        assert worktree.occupied_by == task_holder_id(previous)
        assert extra["attested_state"] == anchor
        assert extra["checkout_custody"]["tenures"][-1]["ended"] == ""
        assert "deferred_exit" not in extra
        Worktree.objects.filter(pk=worktree.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        table = table_with_unreadable_pid(path.parent, nspid="NSpid:\t21\n")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table):
            assert claim_task_checkout_or_park(next_task) is None

        worktree.refresh_from_db()
        assert worktree.get_extra()["attested_state"] == checkout_state(path).digest
        audit = worktree.get_extra()["auto_attestation"]
        assert audit["process_table"]["set_aside"] == ["pid 21 (claude) is unreadable by this uid"]
        assert audit["tenures"][0]["namespace"] == "pid:[42]"
        assert audit["tenures"][0]["ended"] == "abandoned"


class DeferredExitRecoveryTests(_RecoveryCase):
    def setUp(self) -> None:
        super().setUp()
        self.record = {
            "holder": task_holder_id(self.previous),
            "session": "",
            "digest": checkout_state(self.path, ticket=self.ticket).digest,
            "cause": "process_table",
            "at": "2026-10-16T00:00:00+00:00",
        }
        self.worktree.merge_extra(set_keys={"deferred_exit": self.record})
        self.container_table = ProcessTable(
            frozenset(), "/host-proc", (), ("pid 7 (systemd) is unreadable by this uid",)
        )

    def test_next_writer_attests_the_recorded_deferred_exit(self) -> None:
        with patch("teatree.core.worktree.checkout_fence.notify_user") as page:
            self.dispatch(self.container_table)
        page.assert_not_called()
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert self.worktree.get_extra()["attested_state"] == self.record["digest"]
        assert audit["deferred_exit"] == self.record
        assert audit["process_table"] == {
            "source": "/host-proc",
            "gaps": ["pid 7 (systemd) is unreadable by this uid"],
        }
        assert audit["previous"] == self.anchor
        assert audit["holder"] == {"id": task_holder_id(self.next_task), "session": ""}
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit

    def test_cancelled_holder_cannot_use_the_deferred_record(self) -> None:
        Task.objects.filter(pk=self.previous.pk).update(status=Task.Status.FAILED, failure_kind=FailureKind.CANCELLED)
        self.assert_parked(self.container_table)

    def test_claim_path_admits_the_next_writer(self) -> None:
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.container_table):
            assert claim_task_checkout_or_park(self.next_task) is None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.record["digest"]

    def test_worker_path_admits_the_next_writer(self) -> None:
        self.next_task.claim(claimed_by="worker", claimed_by_session="next")
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.container_table),
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=self.container_table),
            patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)) as drive,
        ):
            runner.run_agent(self.next_task, phase="testing", overlay_skill_metadata={})
        drive.assert_called_once()
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == self.record["digest"]

    def test_other_holder_cannot_use_the_record(self) -> None:
        self.record["holder"] = "task:999"
        self.worktree.merge_extra(set_keys={"deferred_exit": self.record})
        self.assert_parked(self.container_table)

    def test_other_session_cannot_use_the_record(self) -> None:
        self.record["session"] = "other"
        self.worktree.merge_extra(set_keys={"deferred_exit": self.record})
        self.assert_parked(self.container_table)

    def test_intervening_tenure_cannot_use_the_record(self) -> None:
        acquire(self.worktree, holder="operator", holder_session="terminal")
        release(self.worktree, holder="operator", holder_session="terminal")
        self.assert_parked(self.container_table)

    def test_state_change_after_deferral_refuses(self) -> None:
        (self.path / "later.txt").write_text("later")
        self.assert_parked(self.container_table)

    def test_unusable_table_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset(), "", ("unavailable",)))

    def test_process_still_in_checkout_refuses(self) -> None:
        self.assert_parked(ProcessTable(frozenset({self.path}), "/host-proc"))

    def test_direct_attestation_rejects_unusable_table(self) -> None:
        acquire(self.worktree, holder=task_holder_id(self.next_task))
        assert not attest_deferred_exit(
            self.next_task, self.worktree, checkout_state(self.path, ticket=self.ticket), ProcessTable(frozenset(), "")
        )

    def test_direct_attestation_rejects_live_process(self) -> None:
        acquire(self.worktree, holder=task_holder_id(self.next_task))
        assert not attest_deferred_exit(
            self.next_task,
            self.worktree,
            checkout_state(self.path, ticket=self.ticket),
            ProcessTable(frozenset({self.path}), "/host-proc"),
        )

    def test_unpersisted_attestation_refuses(self) -> None:
        with patch.object(Worktree, "merge_extra", return_value=None):
            self.assert_parked(self.container_table)
