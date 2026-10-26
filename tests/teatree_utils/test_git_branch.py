from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

from teatree.utils import git_branch
from teatree.utils.git_branch import RefPresence, list_remote_refs, ref_presence, show_ref
from tests._git_repo import make_git_repo, run_git


def _origin_and_clone(tmp_path: Path) -> Path:
    seed = make_git_repo(tmp_path / "seed")
    run_git(tmp_path, "clone", "-q", "--bare", str(seed), str(tmp_path / "origin.git"))
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(clone))
    return clone


def _push_only_to_origin(clone: Path, branch: str) -> None:
    run_git(clone, "checkout", "-q", "-b", branch)
    run_git(clone, "commit", "-q", "--allow-empty", "-m", "work")
    run_git(clone, "push", "-q", "origin", branch)
    run_git(clone, "checkout", "-q", "main")
    run_git(clone, "branch", "-q", "-D", branch)
    run_git(clone, "update-ref", "-d", f"refs/remotes/origin/{branch}")


def test_a_branch_in_no_ref_namespace_is_proven_absent(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)

    assert ref_presence(str(clone), "missing", list_remote_refs(str(clone))) is RefPresence.ABSENT


def test_a_local_branch_is_present(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    run_git(clone, "branch", "wip")

    assert ref_presence(str(clone), "wip", list_remote_refs(str(clone))) is RefPresence.PRESENT


def test_a_tracking_ref_is_present_even_after_origin_deletes_its_branch(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    run_git(clone, "update-ref", "refs/remotes/origin/tracked", "HEAD")

    assert ref_presence(str(clone), "tracked", list_remote_refs(str(clone))) is RefPresence.PRESENT


def test_a_branch_known_only_to_origin_is_present(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    _push_only_to_origin(clone, "remote-only")

    assert ref_presence(str(clone), "remote-only", list_remote_refs(str(clone))) is RefPresence.PRESENT


def test_an_unreachable_origin_cannot_prove_absence(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    run_git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    listing = list_remote_refs(str(clone))

    assert listing.heads is None
    assert listing.timed_out is False
    assert ref_presence(str(clone), "missing", listing) is RefPresence.UNKNOWN


def test_a_listing_that_times_out_says_so(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)

    with patch("teatree.utils.git_branch.run_with_status", side_effect=TimeoutExpired("git", 30)):
        listing = list_remote_refs(str(clone))

    assert listing.heads is None
    assert listing.timed_out is True
    assert ref_presence(str(clone), "missing", listing) is RefPresence.UNKNOWN


def test_remote_listing_passes_the_thirty_second_timeout(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)

    with patch("teatree.utils.git_branch.run_with_status", wraps=git_branch.run_with_status) as probe:
        listing = list_remote_refs(str(clone))

    assert listing.heads is not None
    assert probe.call_args.kwargs["timeout"] == 30


def test_a_path_git_cannot_read_proves_nothing(tmp_path: Path) -> None:
    assert show_ref(str(tmp_path), "refs/heads/x") is RefPresence.UNKNOWN
