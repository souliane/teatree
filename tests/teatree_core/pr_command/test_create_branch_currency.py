"""``pr create`` runs the branch-currency gate FIRST (#940).

Placement is load-bearing: a stale base would otherwise poison the
visual-QA gate (it renders the pre-merge tree) and the cold reviewer's
SHA attestation (they would certify a tree missing target-branch
fixes). The gate runs against a real ``git init`` worktree under
``tmp_path`` so the merge behaviour is the actual ``git merge``, not a
mock — anti-vacuous per AGENTS.md § Test-Writing Doctrine.
"""

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree

from ._shared import _MOCK_OVERLAY


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_stale_feature_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """Set up a remote + clone + feature branch where origin/main advanced.

    Returns ``(clone, bare, feature_branch_name)``. The feature branch
    is checked out, and ``origin/main`` has one new commit that does
    not overlap the feature work — so the auto-merge will fast-forward
    / merge cleanly.
    """
    # Bare remote with initial commit.
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(seed, "config", "user.name", "Tester")
    (seed / "a.txt").write_text("base\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "initial")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))

    # Working clone + feature branch with disjoint file.
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    branch = "feature-branch"
    _git(clone, "checkout", "-b", branch)
    (clone / "b.txt").write_text("feature\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-m", "feature")

    # Advance origin/main with a disjoint file (no conflict on merge).
    advance = tmp_path / "advance"
    _git(tmp_path, "clone", str(bare), str(advance))
    _git(advance, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(advance, "config", "user.name", "Tester")
    (advance / "c.txt").write_text("remote-add\n")
    _git(advance, "add", "c.txt")
    _git(advance, "commit", "-m", "remote: add c.txt")
    _git(advance, "push", "origin", "main")

    return clone, bare, branch


class TestPrCreateBranchCurrency(TestCase):
    """`pr create` auto-merges target into branch BEFORE other gates (#940)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_auto_merges_stale_base_before_visual_qa(self) -> None:
        clone, _bare, branch_name = _make_stale_feature_worktree(self.tmp_path)
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch=branch_name,
            extra={"worktree_path": str(clone)},
        )
        pre_sha = _git(clone, "rev-parse", "HEAD")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            call_command("pr", "create", str(ticket.id))

        # Auto-merge landed: HEAD advanced, c.txt is now reachable.
        post_sha = _git(clone, "rev-parse", "HEAD")
        assert post_sha != pre_sha
        assert (clone / "c.txt").exists()
        # Post-merge SHA recorded on the ticket so the cold reviewer
        # attests the post-merge tree.
        ticket.refresh_from_db()
        assert ticket.extra.get("branch_currency_post_merge_sha") == post_sha

    def test_refuses_when_conflict(self) -> None:
        clone, bare, branch_name = _make_stale_feature_worktree(self.tmp_path)
        # Introduce an overlap: modify a.txt on both branch AND remote.
        (clone / "a.txt").write_text("feature-change\n")
        _git(clone, "add", "a.txt")
        _git(clone, "commit", "-m", "feature: change a.txt")
        overlap = self.tmp_path / "overlap"
        _git(self.tmp_path, "clone", str(bare), str(overlap))
        _git(overlap, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(overlap, "config", "user.name", "Tester")
        (overlap / "a.txt").write_text("remote-change\n")
        _git(overlap, "add", "a.txt")
        _git(overlap, "commit", "-m", "remote: change a.txt")
        _git(overlap, "push", "origin", "main")

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch=branch_name,
            extra={"worktree_path": str(clone)},
        )
        pre_sha = _git(clone, "rev-parse", "HEAD")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.id)),
            )

        # Refusal: no merge landed, ticket NOT shipped.
        assert _git(clone, "rev-parse", "HEAD") == pre_sha
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # not shipped
        assert result.get("allowed") is False
        assert "conflict" in str(result.get("error", "")).lower()
        assert result.get("branch") == branch_name
        # Worktree was restored cleanly (merge --abort).
        assert _git(clone, "status", "--porcelain") == ""

    def test_skipped_when_branch_already_current(self) -> None:
        # Set up a non-stale clone (no remote advance).
        seed = self.tmp_path / "seed"
        seed.mkdir()
        _git(seed, "init", "-b", "main")
        _git(seed, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(seed, "config", "user.name", "Tester")
        (seed / "a.txt").write_text("base\n")
        _git(seed, "add", "a.txt")
        _git(seed, "commit", "-m", "initial")
        bare = self.tmp_path / "remote.git"
        _git(self.tmp_path, "clone", "--bare", str(seed), str(bare))
        clone = self.tmp_path / "clone"
        _git(self.tmp_path, "clone", str(bare), str(clone))
        _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
        _git(clone, "config", "user.name", "Tester")
        _git(clone, "checkout", "-b", "feature-branch")
        (clone / "b.txt").write_text("feature\n")
        _git(clone, "add", "b.txt")
        _git(clone, "commit", "-m", "feature")
        pre_sha = _git(clone, "rev-parse", "HEAD")

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch="feature-branch",
            extra={"worktree_path": str(clone)},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            call_command("pr", "create", str(ticket.id))

        # No merge happened — HEAD unchanged; nothing recorded.
        assert _git(clone, "rev-parse", "HEAD") == pre_sha
        ticket.refresh_from_db()
        assert "branch_currency_post_merge_sha" not in (ticket.extra or {})


class TestPrCreateGatesReconcileRenamedBranch(TestCase):
    """#1587: gates evaluate against the branch that actually exists.

    `workspace ticket <N>` mints `Worktree.branch` as `<N>-ticket`; the agent
    renames the git branch to the `<N>-fix-...` convention. The pre-push gates
    read the stale recorded ref, so each gate's `origin/main..<stale>` range
    query failed and was caught fail-soft — silently skipping its check rather
    than evaluating the real branch. `pr create` now reconciles the recorded
    branch to the worktree's actual git branch BEFORE the gates run, so the
    currency gate evaluates (and its auto-merge lands) instead of skipping.
    """

    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def _ticket_with_renamed_branch(self) -> tuple[Ticket, Path, str]:
        clone, _bare, real_branch = _make_stale_feature_worktree(self.tmp_path)
        # The agent renamed the convention-compliant branch; the DB still
        # records the `<N>-ticket` ref minted at `workspace ticket` time.
        renamed = "4242-fix-foo"
        _git(clone, "branch", "-m", real_branch, renamed)
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/4242",
        )
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch="4242-ticket",
            extra={"worktree_path": str(clone)},
        )
        return ticket, clone, renamed

    def test_currency_gate_evaluates_on_renamed_branch(self) -> None:
        ticket, clone, renamed = self._ticket_with_renamed_branch()
        pre_sha = _git(clone, "rev-parse", "HEAD")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            call_command("pr", "create", str(ticket.id))

        # The currency gate evaluated against the real branch: the auto-merge
        # landed (HEAD advanced, c.txt reachable, post-merge SHA recorded) —
        # it did not silently skip on the stale `4242-ticket` ref.
        post_sha = _git(clone, "rev-parse", "HEAD")
        assert post_sha != pre_sha
        assert (clone / "c.txt").exists()
        ticket.refresh_from_db()
        assert ticket.extra.get("branch_currency_post_merge_sha") == post_sha
        # The DB was reconciled to the branch that exists in the worktree.
        assert ticket.worktrees.get().branch == renamed
