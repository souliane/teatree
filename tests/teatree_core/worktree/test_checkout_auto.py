from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.forge_pr_probe import GithubOpenPrHead
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.worktree.checkout_attest import CheckoutAttestation, ask_owner_to_attest
from teatree.core.worktree.checkout_auto import (
    CheckoutAutoProof,
    container_processes_clear,
    prove_checkout_for_rule1,
    resolve_checkout_park,
)
from teatree.core.worktree.checkout_fence import park_task_checkout
from teatree.core.worktree.checkout_state import checkout_state
from teatree.utils.git import run_strict
from tests._owner_channel import tap_on_slack
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


def test_every_container_mount_is_checked_for_a_working_process() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        proc = root / "proc"
        process = proc / "999"
        process.mkdir(parents=True)
        (process / "cwd").symlink_to("/workspace/checkout/subdir")
        answers = {
            ("ps", "-q", "--no-trunc"): "first\nsecond\n",
            ("inspect", "--format", "{{json .Mounts}}", "first"): "[]",
            ("inspect", "--format", "{{json .Mounts}}", "second"): (
                f'[{{"Source": "{root}", "Destination": "/workspace"}}]'
            ),
            ("top", "second", "-eo", "pid"): "PID\n999\n",
        }

        def docker(*args: str) -> str | None:
            return answers.get(args)

        with patch("teatree.core.worktree.checkout_auto._docker", side_effect=docker):
            assert not container_processes_clear(checkout, proc)
            (process / "cwd").unlink()
            (process / "cwd").symlink_to("/another/place")
            assert container_processes_clear(checkout, proc)


def _single_container_answers(root: Path) -> dict[tuple[str, ...], str]:
    return {
        ("ps", "-q", "--no-trunc"): "only\n",
        ("inspect", "--format", "{{json .Mounts}}", "only"): f'[{{"Source": "{root}", "Destination": "/workspace"}}]',
    }


def test_a_container_process_this_view_cannot_read_is_not_ruled_out() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        proc = root / "proc"
        proc.mkdir()
        answers = {**_single_container_answers(root), ("top", "only", "-eo", "pid"): "PID\n999\n"}

        with patch("teatree.core.worktree.checkout_auto._docker", side_effect=lambda *args: answers.get(args)):
            assert not container_processes_clear(checkout, proc)


def test_a_container_process_that_exited_while_being_read_is_clear() -> None:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        checkout.mkdir()
        proc = root / "proc"
        proc.mkdir()
        answers = _single_container_answers(root)
        listings = iter(["PID\n999\n", "PID\n"])

        def docker(*args: str) -> str | None:
            return next(listings) if args[0] == "top" else answers.get(args)

        with patch("teatree.core.worktree.checkout_auto._docker", side_effect=docker):
            assert container_processes_clear(checkout, proc)


class CheckoutAutoProofTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.path, branch="feature", commits_ahead=0)
        run_strict(repo=str(self.path), args=["remote", "add", "origin", "https://github.com/souliane/teatree.git"])
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, phase="coding")
        self.worktree = WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(self.path)})
        self.pr = PullRequestFactory(
            ticket=self.ticket, iid="23", repo="souliane/teatree", url="https://github.com/souliane/teatree/pull/23"
        )
        self.head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        self.pr_head = GithubOpenPrHead(
            "https://github.com/souliane/teatree/pull/23", "souliane/teatree", "feature", self.head
        )

    def test_dirty_checkout_containing_exact_forge_head_is_safe(self) -> None:
        (self.path / "work.txt").write_text("work in progress")
        state = checkout_state(self.path, ticket=self.ticket)
        with (
            patch(
                "teatree.core.worktree.checkout_auto.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            patch("teatree.core.worktree.checkout_auto.container_processes_clear", return_value=True),
            patch("teatree.core.worktree.checkout_auto.forge_pr_probe.github_open_pr_head", return_value=self.pr_head),
            patch(
                "teatree.core.worktree.checkout_auto.forge_pr_probe.github_exact_branch_head", return_value=self.head
            ),
        ):
            proof = prove_checkout_for_rule1(self.task, self.worktree, state)
        assert proof.safe

    def test_clean_checkout_strictly_behind_forge_branch_is_safe_without_an_open_pr(self) -> None:
        (self.path / "new.txt").write_text("forge commit")
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["add", "new.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "forge advanced"])
        forge_head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        run_strict(repo=str(self.path), args=["update-ref", "refs/remotes/origin/feature", forge_head])
        run_strict(repo=str(self.path), args=["reset", "--hard", self.head])
        state = checkout_state(self.path, ticket=self.ticket)
        with (
            patch(
                "teatree.core.worktree.checkout_auto.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            patch("teatree.core.worktree.checkout_auto.container_processes_clear", return_value=True),
            patch("teatree.core.worktree.checkout_auto.forge_pr_probe.github_open_pr_head", return_value=None),
            patch(
                "teatree.core.worktree.checkout_auto.forge_pr_probe.github_exact_branch_head", return_value=forge_head
            ),
        ):
            proof = prove_checkout_for_rule1(self.task, self.worktree, state)
        assert proof.safe
        assert proof.reason == "cleanly behind the forge branch"

    def test_container_process_or_unreadable_inventory_blocks_attestation(self) -> None:
        state = checkout_state(self.path, ticket=self.ticket)
        with (
            patch(
                "teatree.core.worktree.checkout_auto.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            patch("teatree.core.worktree.checkout_auto.container_processes_clear", return_value=False),
        ):
            proof = prove_checkout_for_rule1(self.task, self.worktree, state)
        assert not proof.safe
        assert "container" in proof.reason

    def test_owner_decline_blocks_rule1_attestation_of_the_same_state(self) -> None:
        (self.path / "work.txt").write_text("work in progress")
        state = checkout_state(self.path, ticket=self.ticket)
        card = ask_owner_to_attest(self.task, CheckoutAttestation(self.worktree, state, host_evidence=True))
        assert card is not None
        assert tap_on_slack(card, "Decline")
        with patch("teatree.core.worktree.checkout_fence.notify_user"):
            park_task_checkout(self.task, reason="owner declined checkout state")

        with patch(
            "teatree.core.worktree.checkout_auto.prove_checkout_for_rule1",
            return_value=CheckoutAutoProof(safe=True, reason="contains the pull request head"),
        ):
            proof, _marker = resolve_checkout_park(self.task, self.worktree, state)

        self.worktree.refresh_from_db()
        assert not proof.safe
        assert self.worktree.get_extra().get("attested_state") != state.digest

    def test_replayed_checkout_decision_restores_the_attested_state(self) -> None:
        state = checkout_state(self.path, ticket=self.ticket)
        with patch("teatree.core.worktree.checkout_fence.notify_user"):
            park_task_checkout(self.task, reason="checkout changed")
        with patch(
            "teatree.core.worktree.checkout_auto.prove_checkout_for_rule1",
            return_value=CheckoutAutoProof(safe=True, reason="contains the pull request head"),
        ):
            first, marker = resolve_checkout_park(self.task, self.worktree, state)
            assert first.safe
            assert DeferredQuestion.objects.get(dedupe_marker=marker).applied_at is not None
            self.worktree.merge_extra(set_keys={"attested_state": "other"})
            next_task = TaskFactory(ticket=self.ticket, phase="coding")
            with patch("teatree.core.worktree.checkout_fence.notify_user"):
                park_task_checkout(next_task, reason="checkout changed")
            replayed, _marker = resolve_checkout_park(next_task, self.worktree, state)

        self.worktree.refresh_from_db()
        next_task.refresh_from_db()
        assert replayed.safe
        assert self.worktree.get_extra()["attested_state"] == state.digest
        assert next_task.not_before is None

    def test_conflict_marker_blocks_attestation(self) -> None:
        (self.path / "work.txt").write_text("<<<<<<< ours\nchange\n=======\nother\n>>>>>>> theirs\n")
        state = checkout_state(self.path, ticket=self.ticket)
        with (
            patch(
                "teatree.core.worktree.checkout_auto.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            patch("teatree.core.worktree.checkout_auto.container_processes_clear", return_value=True),
            patch("teatree.core.worktree.checkout_auto.forge_pr_probe.github_open_pr_head", return_value=self.pr_head),
            patch(
                "teatree.core.worktree.checkout_auto.forge_pr_probe.github_exact_branch_head", return_value=self.head
            ),
        ):
            proof = prove_checkout_for_rule1(self.task, self.worktree, state)
        assert not proof.safe
        assert "conflict" in proof.reason

    def test_committed_conflict_marker_blocks_attestation(self) -> None:
        (self.path / "conflict.txt").write_text("<<<<<<< ours\nchange\n=======\nother\n>>>>>>> theirs\n")
        run_strict(repo=str(self.path), args=["config", "user.email", "test@example.com"])
        run_strict(repo=str(self.path), args=["config", "user.name", "Test"])
        run_strict(repo=str(self.path), args=["add", "conflict.txt"])
        run_strict(repo=str(self.path), args=["commit", "-m", "committed marker"])
        head = run_strict(repo=str(self.path), args=["rev-parse", "HEAD"])
        pr_head = GithubOpenPrHead(self.pr_head.url, self.pr_head.repository, self.pr_head.branch, head)
        state = checkout_state(self.path, ticket=self.ticket)
        with (
            patch(
                "teatree.core.worktree.checkout_auto.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
            ),
            patch("teatree.core.worktree.checkout_auto.container_processes_clear", return_value=True),
            patch("teatree.core.worktree.checkout_auto.forge_pr_probe.github_open_pr_head", return_value=pr_head),
            patch("teatree.core.worktree.checkout_auto.forge_pr_probe.github_exact_branch_head", return_value=head),
        ):
            proof = prove_checkout_for_rule1(self.task, self.worktree, state)
        assert not proof.safe
        assert "conflict" in proof.reason
