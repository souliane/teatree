"""``ticket clear`` refuses a CLEAR only on a real merge conflict (#940).

Branch-currency is **conflict-only**: a ``reviewed_sha`` that merely
trails the target branch still clears (GitHub re-applies its diff onto
the live target at squash-merge time, and the merge-time SHA + live-CI
re-checks still guard correctness). The CLEAR is refused only when the
reviewed SHA both trails the target AND the merge produces conflicts an
automatic squash-merge could not resolve. The pre-flight runs BEFORE
:meth:`MergeClear.issue`. Real ``git init`` under ``tmp_path`` exercises
the actual ``merge-tree`` prediction — no mocks on the git layer.
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


def _seed_remote(tmp_path: Path) -> Path:
    """Bare remote with one commit on ``main`` containing ``a.txt``."""
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
    return bare


def _advance_remote(tmp_path: Path, bare: Path, *, filename: str, content: str) -> None:
    work = tmp_path / f"advance-{filename}"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    (work / filename).write_text(content)
    _git(work, "add", filename)
    _git(work, "commit", "-m", f"remote: add {filename}")
    _git(work, "push", "origin", "main")


def _make_behind_clean_sha(tmp_path: Path) -> tuple[Path, str]:
    """Clone whose ``feature-branch`` SHA trails ``origin/main`` but merges clean.

    The feature touches ``b.txt``; the target advances with ``c.txt`` —
    no overlap, so the merge is conflict-free. Returns ``(clone,
    feature_sha)``.
    """
    bare = _seed_remote(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    _git(clone, "checkout", "-b", "feature-branch")
    (clone / "b.txt").write_text("feature\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-m", "feature")
    feature_sha = _git(clone, "rev-parse", "HEAD")

    _advance_remote(tmp_path, bare, filename="c.txt", content="remote-add\n")
    _git(clone, "fetch", "origin")
    return clone, feature_sha


def _make_behind_conflicting_sha(tmp_path: Path) -> tuple[Path, str]:
    """Clone whose ``feature-branch`` SHA trails AND conflicts with target.

    Both the feature and the target edit ``a.txt`` — the merge cannot be
    resolved automatically. Returns ``(clone, feature_sha)``.
    """
    bare = _seed_remote(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(clone, "config", "user.name", "Tester")
    _git(clone, "checkout", "-b", "feature-branch")
    (clone / "a.txt").write_text("feature-change\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "feature: change a.txt")
    feature_sha = _git(clone, "rev-parse", "HEAD")

    # Target edits the same file → conflict.
    work = tmp_path / "advance-overlap"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@e.st")  # privacy-scan:allow
    _git(work, "config", "user.name", "Tester")
    (work / "a.txt").write_text("remote-change\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "remote: change a.txt")
    _git(work, "push", "origin", "main")

    _git(clone, "fetch", "origin")
    return clone, feature_sha


class _SafeReview:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        _ = changed_files
        return False


class _SafeOverlay:
    """Non-impacting overlay double — keeps the orthogonal #1967 E2E gate inert."""

    review = _SafeReview()


class TestTicketClearBranchCurrency(TestCase):
    """`ticket clear` refuses a CLEAR only on a real merge conflict (#940)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        # The #1967 mandatory-E2E gate also runs at `ticket clear`; pin a
        # non-impacting overlay so these branch-currency tests exercise only
        # the #940 currency behaviour they are about.
        monkeypatch.setattr("teatree.core.gates.e2e_mandatory_gate.get_overlay", lambda *_a, **_k: _SafeOverlay())

    def _attach_ticket(self, clone: Path, branch: str) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(clone),
            branch=branch,
            extra={"worktree_path": str(clone)},
        )
        return ticket

    def test_allows_clear_for_behind_but_mergeable_sha(self) -> None:
        """The core requirement: behind-but-conflict-free SHA clears without rebase."""
        clone, behind_sha = _make_behind_clean_sha(self.tmp_path)
        ticket = self._attach_ticket(clone, "feature-branch")

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "999",
                "souliane/teatree",
                reviewed_sha=behind_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is True, f"behind-but-clean CLEAR refused: {result}"

    def test_refuses_clear_for_conflicting_sha(self) -> None:
        clone, conflicting_sha = _make_behind_conflicting_sha(self.tmp_path)
        ticket = self._attach_ticket(clone, "feature-branch")

        result = cast(
            "dict[str, object]",
            call_command(
                "ticket",
                "clear",
                "998",
                "souliane/teatree",
                reviewed_sha=conflicting_sha,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        assert result.get("issued") is False
        error = str(result.get("error", ""))
        assert "conflict" in error.lower()
        assert "a.txt" in error

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
                reviewed_sha="c" * 40,
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                blast_class="logic",
                ticket_id=int(ticket.pk),
            ),
        )

        # What we PIN here is that the branch-currency check itself did
        # not block on conflict grounds.
        error = str(result.get("error", ""))
        assert "conflict" not in error.lower()
