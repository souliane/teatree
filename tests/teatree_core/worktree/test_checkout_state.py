from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from django.test import TestCase

from teatree.core.worktree.checkout_state import checkout_digest, checkout_state
from teatree.utils.git import run_strict
from teatree.utils.run import CommandFailedError
from tests.factories import TicketFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


def test_checkout_state_detects_untracked_content_and_tracked_edits(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
    clean = checkout_state(checkout)
    assert clean.pristine

    untracked = checkout / "new.txt"
    untracked.write_text("first")
    first = checkout_state(checkout)
    assert not first.pristine
    assert first.digest != clean.digest

    untracked.write_text("second")
    second = checkout_state(checkout)
    assert second.digest != first.digest

    untracked.unlink()
    (checkout / "README.md").write_text("edited\n")
    edited = checkout_state(checkout)
    assert edited.digest != clean.digest

    (checkout / "README.md").write_text("edited differently\n")
    assert checkout_state(checkout).digest != edited.digest, "same status, different tracked content"


def test_clean_digest_matches_a_head_and_branch_read(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
    head = run_strict(repo=str(checkout), args=["rev-parse", "HEAD"])
    assert checkout_state(checkout).digest == checkout_digest(head, "feature")


def test_checkout_state_counts_each_changed_path_once(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
    assert checkout_state(checkout).uncommitted == 0

    run_strict(repo=str(checkout), args=["mv", "README.md", "RENAMED.md"])
    (checkout / "new.txt").write_text("untracked")

    assert checkout_state(checkout).uncommitted == 2


def test_checkout_state_counts_commits_ahead(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=3)
    state = checkout_state(checkout)
    assert state.commits_ahead == 3
    assert not state.pristine


def test_checkout_state_accepts_detached_head(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=1)
    run_strict(repo=str(checkout), args=["checkout", "--detach", "HEAD"])
    state = checkout_state(checkout)
    assert state.commits_ahead == 1
    assert state.digest


class StackedTicketTests(TestCase):
    def test_a_stacked_ticket_counts_commits_from_its_target_branch(self) -> None:
        with TemporaryDirectory() as temp:
            checkout = Path(temp) / "checkout"
            _init_repo_with_branch(checkout, branch="parent", commits_ahead=2)
            run_strict(repo=str(checkout), args=["checkout", "-b", "feature"])
            run_strict(repo=str(checkout), args=["remote", "add", "origin", str(checkout)])
            run_strict(repo=str(checkout), args=["fetch", "origin"])
            run_strict(repo=str(checkout), args=["remote", "set-head", "origin", "main"])
            stacked = TicketFactory(extra={"target_branch": "parent"})

            assert checkout_state(checkout, ticket=stacked).pristine
            assert checkout_state(checkout).commits_ahead == 2


def test_dangling_symlinks_and_nested_repos_digest_cleanly(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=0)
    (checkout / "dangling").symlink_to(tmp_path / "missing")
    _init_repo_with_branch(checkout / "nested", branch="feature", commits_ahead=0)
    first = checkout_state(checkout)

    (checkout / "dangling").unlink()
    (checkout / "dangling").symlink_to(tmp_path / "elsewhere")
    assert checkout_state(checkout).digest != first.digest


def test_unreadable_git_refuses(tmp_path: Path) -> None:
    with pytest.raises(CommandFailedError):
        checkout_state(tmp_path)
