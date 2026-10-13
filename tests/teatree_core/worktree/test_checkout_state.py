from pathlib import Path

from teatree.core.worktree.checkout_state import checkout_state
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
    assert checkout_state(checkout).digest != clean.digest


def test_checkout_state_counts_commits_ahead(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _init_repo_with_branch(checkout, branch="feature", commits_ahead=3)
    state = checkout_state(checkout)
    assert state.commits_ahead == 3
    assert not state.pristine
