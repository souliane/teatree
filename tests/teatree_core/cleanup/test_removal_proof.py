"""Checkout-removal proof through real git and persisted worktree rows."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.removal_proof import removal_refusal
from teatree.core.cleanup.unshipped_work import UnshippedWork
from teatree.core.forge_pr_probe import PrProbe
from teatree.core.models import Ticket, Worktree
from teatree.utils import git
from teatree.utils.git_sync import RemoteReadError
from teatree.utils.git_sync import fetch_branch as real_fetch_branch
from tests.teatree_core.cleanup._shared import _run_git, forge_reporting


class TestRemovalProof(TestCase):
    @pytest.fixture(autouse=True)
    def _checkout(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        self.origin.mkdir()
        _run_git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        self.clone = tmp_path / "clone"
        self.clone.mkdir()
        _run_git("init", "-q", "-b", "main", cwd=self.clone)
        _run_git("config", "user.email", "t@t", cwd=self.clone)
        _run_git("config", "user.name", "t", cwd=self.clone)
        _run_git("remote", "add", "origin", str(self.origin), cwd=self.clone)
        (self.clone / "base.txt").write_text("base\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "base", cwd=self.clone)
        _run_git("push", "-q", "-u", "origin", "main", cwd=self.clone)
        self.checkout = tmp_path / "feat"
        _run_git("worktree", "add", "-q", "-b", "feat", str(self.checkout), cwd=self.clone)

    def _row(self, issue: int, state: str) -> Worktree:
        ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{issue}", state=state, repos=["clone"])
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="clone",
            branch="feat",
            extra={"worktree_path": str(self.checkout)},
        )

    def test_open_and_unanswerable_merge_request_both_keep_checkout(self) -> None:
        for probe, expected in (
            (PrProbe.found("https://example.com/pr/1"), "open merge request"),
            (PrProbe.unknown(), "forge cannot say"),
        ):
            with patch("teatree.core.cleanup.removal_proof.find_open_pr_for_branch", return_value=probe):
                assert expected in removal_refusal(self.checkout, clone=self.clone)
        with patch("teatree.core.cleanup.removal_proof.find_open_pr_for_branch", return_value=PrProbe.none()):
            assert removal_refusal(self.checkout, clone=self.clone) == ""

    def test_unanswerable_remote_keeps_clean_checkout(self) -> None:
        with patch("teatree.core.cleanup.unshipped_work.remote_head_shas", side_effect=RemoteReadError("offline")):
            refusal = removal_refusal(self.checkout, clone=self.clone)

        assert "no remote answers" in refusal

    def test_another_in_flight_ticket_sharing_checkout_blocks_removal(self) -> None:
        row = self._row(1, Ticket.State.MERGED)
        self._row(2, Ticket.State.REVIEW_REQUESTED)

        assert "in-flight ticket" in removal_refusal(self.checkout, clone=self.clone, row=row)

    def test_rowless_removal_keeps_checkout_recorded_by_in_flight_ticket(self) -> None:
        self._row(4, Ticket.State.REVIEW_REQUESTED)

        assert "in-flight ticket" in removal_refusal(self.checkout, clone=self.clone)

    def test_empty_directory_is_releasable_only_without_an_in_flight_row(self) -> None:
        empty = self.checkout.parent / "empty"
        empty.mkdir()
        assert removal_refusal(empty, clone=self.clone) == ""

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/5", state=Ticket.State.REVIEW_REQUESTED)
        row = Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="clone", branch="empty", extra={"worktree_path": str(empty)}
        )
        assert "in-flight ticket" in removal_refusal(empty, clone=self.clone)
        assert removal_refusal(empty, clone=self.clone, row=row) == ""
        assert "in-flight ticket" in removal_refusal(empty, clone=self.clone, row=row, sweep=True)
        row.delete()
        (empty / "notes.txt").write_text("keep\n", encoding="utf-8")
        assert "no resolvable HEAD" in removal_refusal(empty, clone=self.clone)

    def test_sweep_keeps_fresh_in_flight_checkout_and_explicit_teardown_clears(self) -> None:
        row = self._row(3, Ticket.State.REVIEW_REQUESTED)

        assert "fresh checkout" in removal_refusal(self.checkout, clone=self.clone, row=row, sweep=True)
        assert removal_refusal(self.checkout, clone=self.clone, row=row) == ""

    def test_stale_tracking_ref_does_not_clear_unshipped_commit(self) -> None:
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("push", "-q", "origin", "feat", cwd=self.checkout)
        _run_git("update-ref", "-d", "refs/heads/feat", cwd=self.origin)

        assert "no live remote ref" in removal_refusal(self.checkout, clone=self.clone)

    def test_stale_default_ref_does_not_prove_a_commit_landed(self) -> None:
        base = git.run_strict(repo=str(self.clone), args=["rev-parse", "HEAD"])
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--ff-only", "feat", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)
        _run_git("update-ref", "refs/heads/main", base, cwd=self.origin)

        assert "no live remote ref" in removal_refusal(self.checkout, clone=self.clone)

    def test_reverted_default_branch_does_not_prove_a_commit_still_landed(self) -> None:
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--squash", "feat", cwd=self.clone)
        _run_git("commit", "-q", "-m", "squash feature", cwd=self.clone)
        _run_git("revert", "--no-edit", "HEAD", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)

        assert "no live remote ref" in removal_refusal(self.checkout, clone=self.clone)

    def test_commit_landed_on_live_default_branch_can_be_removed(self) -> None:
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--ff-only", "feat", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)

        assert removal_refusal(self.checkout, clone=self.clone) == ""

    def test_lagging_tracking_ref_refreshes_before_landed_proof(self) -> None:
        old_target = git.run_strict(repo=str(self.clone), args=["rev-parse", "origin/main"])
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--squash", "feat", cwd=self.clone)
        _run_git("commit", "-q", "-m", "squash feature", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)
        _run_git("update-ref", "refs/remotes/origin/main", old_target, cwd=self.clone)

        assert removal_refusal(self.checkout, clone=self.clone) == ""
        assert git.run_strict(repo=str(self.clone), args=["rev-parse", "origin/main"]) != old_target

    def test_edit_arriving_during_target_refresh_keeps_checkout(self) -> None:
        old_target = git.run_strict(repo=str(self.clone), args=["rev-parse", "origin/main"])
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--squash", "feat", cwd=self.clone)
        _run_git("commit", "-q", "-m", "squash feature", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)
        _run_git("update-ref", "refs/remotes/origin/main", old_target, cwd=self.clone)

        def edit_during_fetch(repo: str, branch: str, remote: str = "origin") -> None:
            (self.checkout / "late.txt").write_text("keep\n", encoding="utf-8")
            real_fetch_branch(repo, branch, remote=remote)

        with patch("teatree.core.cleanup.removal_proof.fetch_branch", side_effect=edit_during_fetch):
            refusal = removal_refusal(self.checkout, clone=self.clone)

        assert "late.txt" in refusal

    def test_dangling_head_uses_recovered_commit_for_landed_proof(self) -> None:
        (self.checkout / "feature.txt").write_text("new\n", encoding="utf-8")
        _run_git("add", "feature.txt", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        _run_git("merge", "-q", "--squash", "feat", cwd=self.clone)
        _run_git("commit", "-q", "-m", "squash feature", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)
        _run_git("update-ref", "-d", "refs/heads/feat", cwd=self.clone)

        assert git.current_branch(str(self.checkout)) == git.DETACHED_HEAD
        assert removal_refusal(self.checkout, clone=self.clone) == ""

    def test_merged_tip_uses_checked_out_branch_name(self) -> None:
        (self.checkout / "x.txt").write_text("feature\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.checkout)
        _run_git("commit", "-q", "-m", "feature", cwd=self.checkout)
        tip = git.run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        (self.clone / "x.txt").write_text("resolved feature\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "squash feature", cwd=self.clone)
        (self.clone / "x.txt").write_text("rewritten later\n", encoding="utf-8")
        _run_git("add", "-A", cwd=self.clone)
        _run_git("commit", "-q", "-m", "rewrite", cwd=self.clone)
        _run_git("push", "-q", "origin", "main", cwd=self.clone)

        with (
            forge_reporting(merged_head_sha=tip),
            patch(
                "teatree.core.worktree.branch_classification._merged_pr_head_sha",
                side_effect=lambda repo, branch: tip if branch == "feat" else "",
            ),
        ):
            assert removal_refusal(self.checkout, clone=self.clone) == ""

    def test_recovery_uses_typed_missing_head_even_if_message_changes(self) -> None:
        sha = git.run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        with (
            patch(
                "teatree.core.cleanup.removal_proof.probe_unshipped_work",
                side_effect=[UnshippedWork(unreadable="message changed", head_missing=True), UnshippedWork()],
            ) as probe,
            patch("teatree.core.cleanup.removal_proof.git.recovered_head_sha_after_ref_gone", return_value=sha),
            patch("teatree.core.cleanup.removal_proof.find_open_pr_for_branch", return_value=PrProbe.none()),
        ):
            assert removal_refusal(self.checkout, clone=self.clone) == ""

        assert probe.call_args_list[1].kwargs["base_ref"] == sha
