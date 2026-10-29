"""What marks a checkout as evidence a reclaim must leave alone, read from the checkout itself (#5615)."""

from pathlib import Path

import pytest

from teatree.core.cleanup.checkout_evidence import evidence_checkout_reason
from tests._git_repo import make_git_repo, plant_review_evidence_receipt, run_git


def test_an_ordinary_checkout_is_not_evidence(tmp_path: Path) -> None:
    checkout = make_git_repo(tmp_path / "ordinary")
    run_git(checkout, "checkout", "-q", "-b", "feature/plain")

    assert evidence_checkout_reason(checkout) == ""


def test_a_receipt_marks_the_checkout_whatever_its_branch(tmp_path: Path) -> None:
    checkout = make_git_repo(tmp_path / "reviewed")
    plant_review_evidence_receipt(checkout)

    assert "review-evidence receipt" in evidence_checkout_reason(checkout)


@pytest.mark.parametrize("branch", ["evidence-run", "rescue/old", "pre-rewrite-BACKUP"])
def test_a_branch_named_for_evidence_rescue_or_backup_marks_the_checkout(tmp_path: Path, branch: str) -> None:
    checkout = make_git_repo(tmp_path / "marked")
    run_git(checkout, "checkout", "-q", "-b", branch)

    assert branch in evidence_checkout_reason(checkout)


def test_the_branch_a_stale_row_recorded_marks_the_checkout_too(tmp_path: Path) -> None:
    checkout = make_git_repo(tmp_path / "stale")

    assert "backup/x" in evidence_checkout_reason(checkout, recorded_branch="backup/x")


def test_a_detached_head_is_not_a_branch_marker(tmp_path: Path) -> None:
    checkout = make_git_repo(tmp_path / "detached")
    run_git(checkout, "checkout", "-q", "--detach")

    assert evidence_checkout_reason(checkout) == ""


def test_a_directory_git_cannot_read_is_treated_as_evidence(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    assert "could not be read" in evidence_checkout_reason(not_a_repo)
