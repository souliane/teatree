import json
import os
import shlex
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.agents import runner
from teatree.core import forge_pr_probe
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.checkout_fence import claim_task_checkout_or_park, verify_task_checkout
from teatree.core.worktree.checkout_released import reattest_released_pr_head
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import acquire, release, task_holder_id
from teatree.mcp.serializers import serialize_worktree
from teatree.utils.git import check, run_strict
from tests._process_table_venue import table_with_unreadable_pid
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


@dataclass(frozen=True)
class ForgeResponse:
    head_repo: str = "souliane/teatree"
    head_branch: str = "feature"
    forge_head: str | None = None
    remote_head: str | None = None
    pr: PrProbe | None = None


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

    def forge_stdout(
        self,
        cmd: list[str],
        _repo: str,
        response: ForgeResponse | None = None,
    ) -> str | None:
        response = response or ForgeResponse()
        if cmd[1:3] == ["pr", "list"]:
            found = response.pr if response.pr is not None else PrProbe.found(self.pr.url)
            if not found.is_found or cmd[-1] != "feature":
                return "[]"
            return json.dumps(
                [
                    {
                        "url": found.url,
                        "headRepository": {
                            "id": "R_kgDORe_NCA",
                            "name": "teatree",
                            "nameWithOwner": response.head_repo,
                        },
                        "headRefName": response.head_branch,
                        "headRefOid": response.forge_head or self.head,
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
        remote = response.remote_head or run_strict(repo=str(self.origin), args=["rev-parse", "refs/heads/feature"])
        self.on_remote_read()
        return json.dumps({"ref": "refs/heads/feature", "object": {"sha": remote}})

    def claim(
        self,
        table: ProcessTable | None = None,
        *,
        response: ForgeResponse | None = None,
    ) -> TaskAttempt | None:
        def forge_stdout(cmd: list[str], repo: str) -> str | None:
            return self.forge_stdout(cmd, repo, response)

        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=table or self.clear),
            patch.object(forge_pr_probe, "_github_cli_stdout", side_effect=forge_stdout),
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

    def direct_recovery(self, table: ProcessTable | None = None) -> bool:
        with patch.object(forge_pr_probe, "_github_cli_stdout", side_effect=self.forge_stdout):
            return reattest_released_pr_head(
                self.claimant, self.worktree, checkout_state(self.path, ticket=self.ticket), table or self.clear
            )

    @contextmanager
    def git_shim(self, guard: str) -> Generator[Path]:
        real_git = shutil.which("git")
        assert real_git is not None
        bin_dir = self.path.parent / "git-bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim.write_text(f'#!/bin/sh\n{guard}exec {shlex.quote(real_git)} "$@"\n')
        shim.chmod(0o755)
        with patch.dict("os.environ", {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            yield bin_dir

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

    def test_released_custody_reads_prior_tasks_once(self) -> None:
        with CaptureQueriesContext(connection) as queries:
            assert self.claim() is None
        task_reads = [query for query in queries if "SELECT" in query["sql"] and Task._meta.db_table in query["sql"]]
        assert len(task_reads) == 1

    def test_empty_custody_refuses_direct_recovery(self) -> None:
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"] = []
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        self.worktree.refresh_from_db()
        assert not self.direct_recovery()

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

    def test_ended_current_tenure_refuses_recovery(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        extra = self.worktree.get_extra()
        current = extra["checkout_custody"]["tenures"][-1]
        current["ended"] = "released"
        current["ended_at"] = current["since"]
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        assert not self.direct_recovery()

    def test_occupancy_must_name_claimant(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        Worktree.objects.filter(pk=self.worktree.pk).update(occupied_by=task_holder_id(self.reviewer))
        self.worktree.refresh_from_db()
        assert not self.direct_recovery()

    def test_released_proof_refuses_live_checkout_process(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        busy = ProcessTable(frozenset({self.path}), "/host-proc")
        assert not self.direct_recovery(busy)

    def test_git_failure_parks_and_releases_occupancy(self) -> None:
        original = self.worktree.get_extra()["attested_state"]
        unreadable_reflog = (
            'case " $* " in *" reflog "*) printf "%s\\n" "fatal: unreadable reflog" >&2; exit 128;; esac\n'
        )
        with self.git_shim(unreadable_reflog):
            parked = self.claim()
        assert parked is not None
        self.worktree.refresh_from_db()
        assert self.worktree.occupied_by == ""
        assert self.worktree.get_extra()["attested_state"] == original

    def test_proven_head_must_bind_attested_digest(self) -> None:
        marks = self.path.parent / "git-bin-marks"
        marks.mkdir()
        first_read = marks / "first-head-read"
        substituted = marks / "substituted-head-read"
        substitute_second_head_read = (
            'case " $* " in *" rev-parse --verify HEAD "*)\n'
            f"  if [ -f {shlex.quote(str(first_read))} ]; then\n"
            f"    rm {shlex.quote(str(first_read))}\n"
            f"    touch {shlex.quote(str(substituted))}\n"
            f"    printf '%s\\n' {shlex.quote(self.anchor_head)}\n"
            "    exit 0\n"
            "  fi\n"
            f"  touch {shlex.quote(str(first_read))}\n"
            ";; esac\n"
        )
        with self.git_shim(substitute_second_head_read):
            self.assert_parked(response=ForgeResponse(forge_head=self.anchor_head, remote_head=self.anchor_head))
        assert substituted.exists()

    def test_pr_repository_slug_must_equal_origin(self) -> None:
        foreign = PullRequestFactory(
            ticket=self.ticket, url="https://github.com/other/repo/pull/9001", repo="other/repo", iid="9001"
        )
        self.assert_parked(response=ForgeResponse(pr=PrProbe.found(foreign.url)))

    def test_pr_host_must_equal_origin(self) -> None:
        other_host = PullRequestFactory(
            ticket=self.ticket,
            url="https://mirror.github.com/souliane/teatree/pull/9001",
            repo="souliane/teatree",
            iid="9001",
        )
        self.assert_parked(response=ForgeResponse(pr=PrProbe.found(other_host.url)))

    def test_pr_url_must_parse_as_github(self) -> None:
        self.assert_parked(
            response=ForgeResponse(pr=PrProbe.found("https://github.com/souliane/teatree/not-a-pr/9001"))
        )

    def test_gitlab_shaped_pr_url_on_github_host_parks(self) -> None:
        foreign_shape = PullRequestFactory(
            ticket=self.ticket,
            url="https://github.com/souliane/teatree/-/merge_requests/9001",
            repo="souliane/teatree",
            iid="9001",
        )
        self.assert_parked(response=ForgeResponse(pr=PrProbe.found(foreign_shape.url)))

    def test_reported_pr_source_branch_must_equal_checkout(self) -> None:
        self.assert_parked(response=ForgeResponse(head_branch="other"))

    def test_t2_checkout_off_its_recorded_branch_parks(self) -> None:
        with self.subTest("recorded_branch_must_equal_checkout"):
            Worktree.objects.filter(pk=self.worktree.pk).update(branch="other")
            self.assert_parked()
            Worktree.objects.filter(pk=self.worktree.pk).update(branch="feature")
        with self.subTest("detached_head"):
            self.assert_clean_claim_then_restore()
            run_strict(repo=str(self.path), args=["checkout", "--detach", self.head])
            self.assert_parked()

    def test_t1_released_shipping_and_reviewer_claim_at_owned_pr_head(self) -> None:
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        assert table.eviction_refuse_reason(nested_writer=True) == ""
        assert self.claim(table) is None
        self.worktree.refresh_from_db()
        audit = self.worktree.get_extra()["auto_attestation"]
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.path, ticket=self.ticket).digest
        assert audit["anchor_commit"] == self.anchor_head
        assert audit["current_head"] == self.head
        assert audit["pr_head"] == self.head
        assert audit["remote_head"] == self.head
        assert audit["pull_request"] == self.pr.url
        assert audit["previous"] == self.anchor
        assert audit["holder"] == {"id": task_holder_id(self.claimant), "session": self.claimant.claimed_by_session}
        assert audit["process_table"]["set_aside"] == ["pid 21 (claude) is unreadable by this uid"]
        assert [entry["holder"] for entry in audit["tenures"]] == [
            task_holder_id(self.shipping),
            task_holder_id(self.reviewer),
            task_holder_id(self.claimant),
        ]
        assert serialize_worktree(self.worktree)["auto_attestation"] == audit

    def test_t3_dirt_parks(self) -> None:
        for kind, filename in (("tracked", "feature.txt"), ("untracked", "untracked.txt")):
            with self.subTest(kind):
                self.assert_clean_claim_then_restore()
                (self.path / filename).write_text("dirty")
                self.assert_parked()
                run_strict(repo=str(self.path), args=["checkout", "--", "."])
                run_strict(repo=str(self.path), args=["clean", "-fdq"])

    def test_t4a_host_gap_with_mixed_empty_and_nested_namespaces_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        extra = self.worktree.get_extra()
        extra["checkout_custody"]["tenures"][0]["namespace"] = ""
        self.worktree.merge_extra(set_keys={"checkout_custody": extra["checkout_custody"]})
        table = table_with_unreadable_pid(self.path.parent, nspid="NSpid:\t21\n")
        assert table.eviction_refuse_reason(nested_writer=False)
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

    def test_t7_anchor_inside_window_after_eleven_commits_attests(self) -> None:
        for index in range(11):
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

    def test_t8_fork_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        self.assert_parked(response=ForgeResponse(head_repo="someone/fork"))

    def test_t8_no_owned_open_pr_parks(self) -> None:
        self.assert_clean_claim_then_restore()
        self.assert_parked(response=ForgeResponse(pr=PrProbe.none()))
        foreign = PullRequestFactory(ticket=TicketFactory())
        self.assert_parked(response=ForgeResponse(pr=PrProbe.found(foreign.url)))

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
        self.assert_parked(response=ForgeResponse(forge_head=self.anchor_head))

    def test_t8_gitlab_origin_never_asks_github_for_a_pr(self) -> None:
        assert acquire(self.worktree, holder=task_holder_id(self.claimant))
        self.worktree.refresh_from_db()
        run_strict(repo=str(self.path), args=["remote", "set-url", "origin", "https://gitlab.com/souliane/teatree.git"])
        with (
            patch.object(forge_pr_probe, "_github_cli_stdout", side_effect=AssertionError("GitLab must not use gh")),
        ):
            assert not reattest_released_pr_head(
                self.claimant, self.worktree, checkout_state(self.path, ticket=self.ticket), self.clear
            )
