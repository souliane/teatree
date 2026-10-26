"""Matching worktrees and unreadable forge reads keep held reviews safe."""

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from teatree.core.models import Ticket
from teatree.loop.shipping_hold_evidence import Evidence, disk_evidence, forge_evidence


def _git(path: Path, *args: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run([executable, "-C", str(path), *args], check=True, capture_output=True)


def _ticket() -> Ticket:
    return Ticket(
        overlay="test",
        issue_url="https://github.com/souliane/teatree/issues/5346",
        issue_number="5346",
        extra={},
    )


def test_matching_checkout_holds_and_other_project_does_not() -> None:
    ticket = _ticket()
    with TemporaryDirectory(prefix="5346-root-") as directory:
        root = Path(directory)
        path = root / "5346-ticket" / "teatree"
        path.mkdir(parents=True)
        _git(path, "init", "-b", "main")
        _git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-m", "base")
        _git(path, "checkout", "-b", "5346-work")
        _git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "--allow-empty", "-m", "work")
        _git(path, "remote", "add", "origin", "https://github.com/souliane/teatree.git")
        with patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=root):
            assert str(path) in disk_evidence(ticket).found
            _git(path, "remote", "set-url", "origin", "https://github.com/other/project.git")
            unrelated = root / "other" / "teatree"
            unrelated.mkdir(parents=True)
            _git(unrelated, "init", "-b", "main")
            _git(
                unrelated,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "base",
            )
            _git(unrelated, "checkout", "-b", "unrelated")
            _git(
                unrelated,
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "--allow-empty",
                "-m",
                "work",
            )
            _git(unrelated, "remote", "add", "origin", "https://github.com/souliane/teatree.git")
            assert disk_evidence(ticket).found == ""


def test_unreadable_matching_checkout_is_unverified_and_unrelated_one_is_ignored() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "unrelated" / ".git").mkdir(parents=True)
        with patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=root):
            assert disk_evidence(_ticket()) == Evidence()
            (root / "5346-ticket" / ".git").mkdir(parents=True)
            assert "git (" in disk_evidence(_ticket()).unverified


def test_unreadable_branch_with_recorded_branch_is_unverified() -> None:
    ticket = _ticket()
    ticket.extra = {"branch": "5346-work"}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "workspace" / "teatree" / ".git").mkdir(parents=True)
        with patch("teatree.loop.shipping_hold_evidence.worktree_root", return_value=root):
            assert "git (" in disk_evidence(ticket).unverified


def test_failed_forge_read_is_unverified() -> None:
    ticket = _ticket()
    host = Mock()
    host.list_unshipped_branches.side_effect = RuntimeError("HTTP 500")
    with (
        patch("teatree.loop.shipping_hold_evidence.get_overlay", return_value=object()),
        patch("teatree.loop.shipping_hold_evidence.get_code_host_for_url", return_value=host),
    ):
        assert "forge (RuntimeError: HTTP 500)" in forge_evidence(ticket).unverified


def test_no_issue_url_is_unverified() -> None:
    assert "issue URL" in forge_evidence(Ticket(overlay="test", issue_url="", issue_number="")).unverified
