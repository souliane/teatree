"""``ticket clear`` refuses a CLEAR for a stale ``reviewed_sha`` (#940).

The CLEAR is the cold reviewer's attestation; binding it to a SHA
whose merge-base trails the target branch means the release pipeline
later certifies a tree missing target-branch fixes. The pre-flight
runs BEFORE :meth:`MergeClear.issue` so the orchestrator's
``reviewed_sha`` always points at the post-merge SHA. Real ``git
init`` under ``tmp_path`` exercises the actual ``rev-list`` check —
no mocks on the git layer.
"""

import subprocess
from pathlib import Path
from typing import cast

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Ticket, Worktree


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_clone_with_stale_sha(tmp_path: Path) -> tuple[Path, str, str]:
    """Set up a clone where ``feature`` SHA trails ``origin/main``.

    Returns ``(clone, feature_sha, current_sha)``: ``feature_sha`` is
    the SHA the orchestrator might (wrongly) pass as ``reviewed_sha``,
    ``current_sha`` is the post-merge SHA the cold reviewer should
    actually attest.
    """
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

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    _git(clone, "checkout", "-b", "feature-branch")
    (clone / "b.txt").write_text("feature\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-m", "feature")
    feature_sha = _git(clone, "rev-parse", "HEAD")

    # Advance origin/main.
    advance = tmp_path / "advance"
    _git(tmp_path, "clone", str(bare), str(advance))
    _git(advance, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(advance, "config", "user.name", "Tester")
    (advance / "c.txt").write_text("remote-add\n")
    _git(advance, "add", "c.txt")
    _git(advance, "commit", "-m", "remote: c.txt")
    _git(advance, "push", "origin", "main")

    # Force a fetch so the clone sees the new origin/main.
    _git(clone, "fetch", "origin")
    current_sha = _git(clone, "rev-parse", "origin/main")
    return clone, feature_sha, current_sha


class TestTicketClearBranchCurrency(TestCase):
    """`ticket clear` refuses a stale ``reviewed_sha`` (#940)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_refuses_clear_for_sha_behind_target(self) -> None:
        clone, stale_sha, _current = _make_clone_with_stale_sha(self.tmp_path)
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch="feature-branch",
            extra={"worktree_path": str(clone)},
        )

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "999",
                "souliane/teatree",
                stale_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is False
        error = str(result.get("error", ""))
        assert "behind" in error.lower()
        assert "origin/main" in error or "merge target" in error.lower()

    def test_allows_clear_for_current_sha(self) -> None:
        """A CLEAR for a SHA reachable from target must be allowed."""
        clone, _stale, current_sha = _make_clone_with_stale_sha(self.tmp_path)
        # Fast-forward the working clone so HEAD == origin/main.
        _git(clone, "checkout", "main")
        _git(clone, "pull", "--ff-only", "origin", "main")

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch="main",
            extra={"worktree_path": str(clone)},
        )

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "888",
                "souliane/teatree",
                current_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is True, f"current-SHA CLEAR refused unexpectedly: {result}"

    def test_no_worktree_skips_currency_check(self) -> None:
        """Without a worktree to verify against, the check is skipped (do-not-block)."""
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        # No Worktree attached.

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "777",
                "souliane/teatree",
                "c" * 40,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        # Either the CLEAR is issued (skip the check) or the underlying
        # MergeClear.issue refuses on its own grounds — what we PIN here
        # is that the branch-currency check itself did not block.
        error = str(result.get("error", ""))
        assert "behind" not in error.lower()
