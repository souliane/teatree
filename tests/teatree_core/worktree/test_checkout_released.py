import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import TestCase

from teatree.agents import runner
from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.models.checkout_custody import Custody
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park, verify_task_checkout
from teatree.core.worktree.checkout_released import reattest_released_pr_head
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import acquire, release, task_holder_id
from teatree.mcp.serializers import serialize_worktree
from teatree.utils.git import check, run_strict
from tests._process_table_venue import table_with_unreadable_pid
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class ReleasedPrHeadRecoveryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkout"
        self.origin = Path(temporary.name) / "origin.git"
        _init_repo_with_branch(self.path, branch="feature", commits_ahead=0)
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["init", "--bare", str(self.origin)])
        run_strict(repo=str(self.path), args=["remote", "add", "origin", str(self.origin)])
        run_strict(repo=str(self.path), args=["push", "origin", "main", "feature"])
        run_strict(repo=str(self.path), args=["remote", "set-url", "origin", "https://github.com/souliane/teatree.git"])
        run_strict(repo=str(self.path), args=["remote", "set-url", "--push", "origin", str(self.origin)])
        self.ticket = TicketFactory()
        self.shipping = TaskFactory(ticket=self.ticket, phase="shipping")
        self.reviewer = TaskFactory(ticket=self.ticket, phase="reviewing")
        self.claimant = TaskFactory(ticket=self.ticket, phase="coding")
        self.worktree = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path)})
        self.pr = PullRequestFactory(ticket=self.ticket)
        self.clear = ProcessTable(frozenset(), "/host-proc")
        self.on_remote_read = Mock()
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear):
            verify_task_checkout(self.shipping, str(self.path))
        self.anchor = checkout_state(self.path, ticket=self.ticket).digest
        self.anchor_head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        self.shipping.claim(claimed_by="worker", claimed_by_session="ship")
        with (
            patch.object(runner, "nested_pid_namespace", return_value="pid:[42]"),
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=self.clear),
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=ProcessTable(frozenset(), "")),
            patch("teatree.core.worktree.occupancy.finish_owned_checkout_processes", return_value=False),
            patch.object(runner, "_run_agent", side_effect=self.merge_main),
        ):
            runner.run_agent(self.shipping, phase="shipping", overlay_skill_metadata={})
        self.worktree.refresh_from_db()
        assert self.worktree.occupied_by == ""
        assert self.worktree.get_extra()["attested_state"] == self.anchor
        self.reviewer.claim(claimed_by="worker", claimed_by_session="review")
        with (
            patch.object(runner, "nested_pid_namespace", return_value="pid:[43]"),
            patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)),
        ):
            runner.run_agent(self.reviewer, phase="reviewing", overlay_skill_metadata={})
        self.worktree.refresh_from_db()
        assert self.worktree.occupied_by == ""
        assert self.worktree.get_extra()["attested_state"] == self.anchor
        self.head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])

    def merge_main(self, *_args: object, **_kwargs: object) -> TaskAttempt:
        (self.path / "feature.txt").write_text("feature")
        run_strict(repo=str(self.path), args=["add", "feature.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "feature"])
        run_strict(repo=str(self.path), args=["checkout", "main"])
        (self.path / "main.txt").write_text("main")
        run_strict(repo=str(self.path), args=["add", "main.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "main moved"])
        run_strict(repo=str(self.path), args=["checkout", "feature"])
        run_strict(repo=str(self.path), args=["merge", "--no-edit", "main"])
        run_strict(repo=str(self.path), args=["push", "origin", "feature"])
        return Mock(spec=TaskAttempt)

    def claim(
        self,
        table: ProcessTable | None = None,
        *,
        head_repo: str = "souliane/teatree",
        head_branch: str = "feature",
        forge_head: str | None = None,
        pr: PrProbe | None = None,
    ) -> TaskAttempt | None:
        def forge_stdout(cmd: list[str], _repo: str) -> str | None:
            if cmd[1:3] == ["pr", "list"]:
                found = pr if pr is not None else PrProbe.found(self.pr.url)
                if not found.is_found or cmd[-1] != "feature":
                    return "[]"
                return json.dumps(
                    [
                        {
                            "url": found.url,
                            "headRepository": {"nameWithOwner": head_repo},
                            "headRefName": head_branch,
                            "headRefOid": forge_head or self.head,
                        }
                    ]
                )
            if (
                run_strict(repo=str(self.path), args=["remote", "get-url", "origin"])
                != "https://github.com/souliane/teatree.git"
            ):
                return None
            if not check(repo=str(self.origin), args=["show-ref", "--verify", "refs/heads/feature"]):
                return None
            remote = run_strict(repo=str(self.origin), args=["rev-parse", "refs/heads/feature"])
            self.on_remote_read()
            return json.dumps({"ref": "refs/heads/feature", "object": {"sha": remote}})

        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table or self.clear),
            patch.object(forge_pr_probe, "_open_pr_stdout", side_effect=forge_stdout),
            patch("teatree.core.worktree.checkout_fence.ask_owner_to_attest") as card,
            patch("teatree.core.worktree.checkout_fence.notify_user") as page,
        ):
            result = claim_task_checkout_or_park(self.claimant)
        if result is None:
            card.assert_not_called()
            page.assert_not_called()
        return result

    def assert_clean_claim_then_restore(self) -> None:
        self.worktree.refresh_from_db()
        original = deepcopy(self.worktree.extra)
        assert self.claim() is None
        assert release(self.worktree, holder=task_holder_id(self.claimant))
        Worktree.objects.filter(pk=self.worktree.pk).update(extra=original)
        self.worktree.refresh_from_db()

    def assert_parked(self, table: ProcessTable | None = None, **kwargs: object) -> None:
        self.worktree.refresh_from_db()
        recorded = self.worktree.get_extra()["attested_state"]
        assert self.claim(table, **kwargs) is not None
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == recorded

    def direct_recovery(self) -> bool:
        with (
            patch.object(
                forge_pr_probe,
                "github_open_pr_head",
                return_value=(self.pr.url, "souliane/teatree", "feature", self.head),
            ),
            patch.object(forge_pr_probe, "github_exact_branch_head", return_value=self.head),
        ):
            return reattest_released_pr_head(
                self.claimant, self.worktree, checkout_state(self.path, ticket=self.ticket), self.clear
            )

    def test_read_only_prior_tenures_do_not_attest(self) -> None:
        Task.objects.filter(pk=self.shipping.pk).update(phase="reviewing")
        self.assert_parked()

    def test_single_released_writer_can_attest(self) -> None:
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"].pop()
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        assert self.direct_recovery()

    def test_second_state_read_must_match_first(self) -> None:
        def change_checkout() -> None:
            (self.path / "feature.txt").write_text("checkout changed")

        self.on_remote_read.side_effect = change_checkout
        self.assert_parked()
        self.on_remote_read.assert_called_once()

    def test_current_tenure_must_name_claimant(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][-1]["holder"] = task_holder_id(self.reviewer)
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        assert not self.direct_recovery()

    def test_occupancy_must_name_claimant(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        Worktree.objects.filter(pk=self.worktree.pk).update(occupied_by=task_holder_id(self.reviewer))
        self.worktree.refresh_from_db()
        assert not self.direct_recovery()

    def test_proven_head_must_bind_attested_digest(self) -> None:
        real_run = run_strict

        def substitute_head(*, repo: str, args: list[str]) -> str:
            if args == ["rev-parse", "--verify", "HEAD"]:
                return self.anchor_head
            return real_run(repo=repo, args=args)

        with (
            patch("teatree.core.worktree.checkout_released.git.run_strict", side_effect=substitute_head),
            patch.object(
                forge_pr_probe,
                "github_open_pr_head",
                return_value=(self.pr.url, "souliane/teatree", "feature", self.anchor_head),
            ),
            patch.object(forge_pr_probe, "github_exact_branch_head", return_value=self.anchor_head),
        ):
            self.assert_parked()

    def test_pr_repository_slug_must_equal_origin(self) -> None:
        foreign = PullRequestFactory(
            ticket=self.ticket, url="https://github.com/other/repo/pull/9001", repo="other/repo", iid="9001"
        )
        self.assert_parked(pr=PrProbe.found(foreign.url))

    def test_pr_host_must_equal_origin(self) -> None:
        other_host = PullRequestFactory(
            ticket=self.ticket,
            url="https://mirror.github.com/souliane/teatree/pull/9001",
            repo="souliane/teatree",
            iid="9001",
        )
        self.assert_parked(pr=PrProbe.found(other_host.url))

    def test_reported_pr_source_branch_must_equal_checkout(self) -> None:
        self.assert_parked(head_branch="other")

    def test_recorded_branch_must_equal_checkout(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="other")
        self.assert_parked()

    def test_t1_released_shipping_and_reviewer_claim_at_owned_pr_head(self) -> None:
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        assert table.eviction_refuse_reason(nested_writer=True) == ""
        assert self.claim(table) is None
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path, ticket=self.ticket).digest
        assert audit["anchor_commit"] == self.anchor_head
        assert audit["pr_head"] == self.head
        assert audit["remote_head"] == self.head
        assert audit["pull_request"] == self.pr.url
        assert audit["process_table"]["set_aside"] == ["pid 21 (claude) is unreadable by this uid"]
        assert [entry["holder"] for entry in audit["tenures"]] == [
            task_holder_id(self.shipping),
            task_holder_id(self.reviewer),
            task_holder_id(self.claimant),
        ]
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit

    def test_t2_extra_local_commit_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        (self.path / "late.txt").write_text("late")
        run_strict(repo=str(self.path), args=["add", "late.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "late"])
        self.assert_parked()

    def test_t2_different_branch_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        run_strict(repo=str(self.path), args=["checkout", "-b", "other"])
        self.assert_parked()

    def test_t2_detached_head_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        run_strict(repo=str(self.path), args=["checkout", "--detach", self.head])
        self.assert_parked()

    def test_t3_tracked_and_untracked_dirt_park(self) -> None:
        self.assert_clean_claim_then_restore()
        (self.path / "feature.txt").write_text("dirty")
        self.assert_parked()

    def test_t3_untracked_dirt_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        (self.path / "untracked.txt").write_text("dirty")
        self.assert_parked()

    def test_t4a_host_gap_with_mixed_empty_and_nested_namespaces_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = ""
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        assert table.eviction_refuse_reason(nested_writer=False)
        self.assert_parked(table)

    def test_t4a_different_nested_namespaces_set_aside_host_gap(self) -> None:
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        custody = Custody.from_extra(self.worktree.get_extra())
        assert custody is not None
        assert custody.prior_tenures_nested
        assert {entry.namespace for entry in custody.tenures} == {"pid:[42]", "pid:[43]"}
        assert table.eviction_refuse_reason(nested_writer=custody.prior_tenures_nested) == ""
        assert self.claim(table) is None

    def test_t4a_nested_unreadable_gap_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\t7\n")
        assert table.eviction_refuse_reason(nested_writer=True)
        self.assert_parked(table)

    def test_t5_operator_holder_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        acquire(self.worktree, holder="operator", holder_session="terminal")
        release(self.worktree, holder="operator", holder_session="terminal")
        self.assert_parked()

    def test_t5_foreign_ticket_holder_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        foreign = TaskFactory(ticket=TicketFactory(), phase="reviewing")
        acquire(self.worktree, holder=task_holder_id(foreign))
        release(self.worktree, holder=task_holder_id(foreign))
        self.assert_parked()

    def test_t6_completed_cancelled_prior_task_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        Task.objects.filter(pk=self.reviewer.pk).update(
            status=Task.Status.COMPLETED, failure_kind=FailureKind.CANCELLED
        )
        self.assert_parked()

    def test_t6_abandoned_prior_tenure_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][1]["ended"] = "abandoned"
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        self.assert_parked()

    def test_t7_anchor_beyond_bounded_reflog_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        for index in range(70):
            run_strict(repo=str(self.path), args=["commit", "--allow-empty", "-m", f"later {index}"])
        run_strict(repo=str(self.path), args=["push", "origin", "feature"])
        self.head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        self.assert_parked()

    def test_t7_anchor_within_bounded_reflog_attests(self) -> None:
        for index in range(4):
            run_strict(repo=str(self.path), args=["commit", "--allow-empty", "-m", f"later {index}"])
        run_strict(repo=str(self.path), args=["push", "origin", "feature"])
        self.head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        assert self.claim() is None

    def test_t7_missing_anchor_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["anchor"] = "0" * 64
        self.worktree.merge_extra(
            set_keys={
                "attested_state": "0" * 64,
                "checkout_custody": extra["checkout_custody"],
            }
        )
        self.assert_parked()

    def test_t7_dirty_original_anchor_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        (self.path / "temporary.txt").write_text("dirty anchor")
        dirty_digest = checkout_state(self.path, ticket=self.ticket).digest
        (self.path / "temporary.txt").unlink()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["anchor"] = dirty_digest
        self.worktree.merge_extra(
            set_keys={
                "attested_state": dirty_digest,
                "checkout_custody": extra["checkout_custody"],
            }
        )
        self.assert_parked()

    def test_t7_nonancestor_anchor_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        (self.path / "old-tip.txt").write_text("old tip")
        run_strict(repo=str(self.path), args=["add", "old-tip.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "old tip"])
        old_anchor = checkout_state(self.path, ticket=self.ticket).digest
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["anchor"] = old_anchor
        self.worktree.merge_extra(
            set_keys={"attested_state": old_anchor, "checkout_custody": extra["checkout_custody"]}
        )
        run_strict(repo=str(self.path), args=["checkout", "-B", "feature", self.head])
        (self.path / "new-tip.txt").write_text("new tip")
        run_strict(repo=str(self.path), args=["add", "new-tip.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "new tip"])
        new_head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        run_strict(repo=str(self.path), args=["push", "origin", f"{new_head}:refs/heads/proof-candidate"])
        run_strict(repo=str(self.origin), args=["update-ref", "refs/heads/feature", new_head])
        self.head = new_head
        self.assert_parked()

    def test_t8_fork_and_unreadable_remote_park(self) -> None:
        self.assert_clean_claim_then_restore()
        self.assert_parked(head_repo="someone/fork")
        run_strict(repo=str(self.path), args=["remote", "set-url", "origin", str(self.path.parent / "missing.git")])
        self.assert_parked()

    def test_t8_no_owned_open_pr_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        self.assert_parked(pr=PrProbe.none())
        foreign = PullRequestFactory(ticket=TicketFactory())
        self.assert_parked(pr=PrProbe.found(foreign.url))

    def test_t8_missing_exact_remote_ref_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        run_strict(repo=str(self.origin), args=["update-ref", "-d", "refs/heads/feature"])
        self.assert_parked()

    def test_t8_mismatched_remote_head_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        old = run_strict(repo=str(self.path), args=["rev-parse", "main"])
        run_strict(repo=str(self.origin), args=["update-ref", "refs/heads/feature", old])
        self.assert_parked()

    def test_t8_mismatched_pr_head_parks(self) -> None:
        self.assert_parked(forge_head=self.anchor_head)

    def test_t8_gitlab_origin_never_asks_github_for_a_pr(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        run_strict(repo=str(self.path), args=["remote", "set-url", "origin", "https://gitlab.com/souliane/teatree.git"])
        with (
            patch.object(forge_pr_probe, "github_open_pr_head", side_effect=AssertionError("GitLab must not use gh")),
        ):
            assert not reattest_released_pr_head(
                self.claimant, self.worktree, checkout_state(self.path, ticket=self.ticket), self.clear
            )

    def test_other_worktree_with_foreign_open_tenure_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        sibling = self.path.parent / "sibling"
        _init_repo_with_branch(sibling, branch="feature", commits_ahead=0)
        other = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(sibling)})
        sibling_anchor = checkout_state(sibling, ticket=self.ticket).digest
        other.merge_extra(
            set_keys={
                "attested_state": sibling_anchor,
                "checkout_custody": Custody(sibling_anchor, ()).as_dict(),
            }
        )
        acquire(other, holder="operator", holder_session="terminal")
        (sibling / "change.txt").write_text("changed")
        run_strict(repo=str(sibling), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(sibling), args=["config", "user.name", "Test"])
        run_strict(repo=str(sibling), args=["add", "change.txt"])
        run_strict(repo=str(sibling), args=["commit", "-m", "changed"])
        assert self.claim() is not None
        other.refresh_from_db()
        assert other.get_extra()["attested_state"] == sibling_anchor
