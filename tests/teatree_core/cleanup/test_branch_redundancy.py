"""Canonical layered merged-detection against real git under tmp_path (#2763).

The owner's core complaint was that detection short-circuited on the forge
"merged" signal alone. These exercise :func:`branch_redundancy` /
:func:`is_squash_merged` end to end against a real ``main`` clone + bare
``origin``, with the forge probe stubbed so ONLY the deterministic git content
layers decide. Each layer and each invariant has an anti-vacuous case:

- a squash-merged branch (forge absent) is redundant via synthetic-squash (b);
- a fast-forward / plain-merged branch is redundant via cherry-zero / ``--merged``;
- a forge-merged branch whose tip is NOT on target is NOT redundant, tagged
merged_with_post_merge_work with its unique SHAs (never deleted on forge alone);
- a gone-remote (deleted) branch ref never aborts the detection.
"""

import subprocess
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

from teatree.core import branch_classification as bc
from tests.teatree_core.cleanup._shared import _GIT, _clean_env, _run_git


def _init(tmp: Path) -> tuple[Path, Path]:
    """A bare ``origin`` + a ``main`` clone with one base commit pushed to origin/main."""
    remote = tmp / "remote.git"
    subprocess.run(
        [_GIT, "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    work = tmp / "work"
    work.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work)
    _run_git("config", "user.email", "t@t", cwd=work)
    _run_git("config", "user.name", "t", cwd=work)
    _run_git("remote", "add", "origin", str(remote), cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "initial", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    return remote, work


def _branch_with_commit(work: Path, branch: str, fname: str, body: str, subject: str) -> None:
    _run_git("checkout", "-q", "-b", branch, "main", cwd=work)
    (work / fname).write_text(body, encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", subject, cwd=work)
    _run_git("checkout", "-q", "main", cwd=work)


def _no_forge() -> AbstractContextManager[object]:
    return patch.object(bc, "probe_host_cli", return_value="")


def _forge_merged() -> AbstractContextManager[object]:
    return patch.object(bc, "probe_host_cli", return_value="42")


def test_squash_merged_via_b_when_forge_absent_is_redundant(tmp_path: Path) -> None:
    """(b) synthetic-squash: a MULTI-commit squash-merge is redundant w/o the forge.

    Two commits are squashed into one new SHA on main: each original commit's
    patch-id differs from the combined squash, so per-commit ``git cherry`` shows
    them as unique (NOT cherry-zero) — only the synthetic-squash layer (the whole
    current tree-delta as one patch) recognises the branch as fully captured.
    """
    _remote, work = _init(tmp_path)
    _run_git("checkout", "-q", "-b", "feature", "main", cwd=work)
    (work / "a.txt").write_text("first\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "feat: first part", cwd=work)
    (work / "b.txt").write_text("second\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "feat: second part", cwd=work)
    _run_git("checkout", "-q", "main", cwd=work)
    # Squash-merge BOTH commits onto main with a brand-new SHA (the squash), push it.
    _run_git("merge", "-q", "--squash", "feature", cwd=work)
    _run_git("commit", "-q", "-m", "squash: ship it (#1)", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    # Precondition: feature is NOT an ancestor of origin/main (squash made a new SHA).
    not_ancestor = subprocess.run(
        [_GIT, "-C", str(work), "merge-base", "--is-ancestor", "feature", "origin/main"],
        check=False,
        capture_output=True,
        env=_clean_env(),
    ).returncode
    assert not_ancestor != 0

    with _no_forge():
        verdict = bc.branch_redundancy(str(work), "feature")
        assert bc.is_squash_merged(str(work), "feature", "main") is True

    assert verdict.redundant is True
    assert verdict.source == "synthetic-squash"
    assert verdict.forge_merged is False


def test_forge_merged_with_no_new_commits_is_redundant(tmp_path: Path) -> None:
    """A forge-merged branch fast-forwarded onto main (no unique commit) is redundant."""
    _remote, work = _init(tmp_path)
    _branch_with_commit(work, "feature", "f.txt", "ff work\n", "feat: ff")
    _run_git("merge", "-q", "--ff", "feature", cwd=work)  # main now contains feature's commit
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)

    with _forge_merged():
        verdict = bc.branch_redundancy(str(work), "feature")

    assert verdict.redundant is True
    assert verdict.source in {"cherry-zero-unique", "branch-merged"}
    assert verdict.forge_merged is True
    assert verdict.merged_with_post_merge_work is False  # redundant ⇒ never emitted as post-merge


def test_merged_with_post_merge_commits_is_emitted_not_deleted(tmp_path: Path) -> None:
    """forge-merged + post-merge unique content ⇒ NOT redundant, tagged with the SHAs."""
    _remote, work = _init(tmp_path)
    _branch_with_commit(work, "feature", "f.txt", "original\n", "feat: original")
    # Squash the original onto main (the PR merge) …
    _run_git("merge", "-q", "--squash", "feature", cwd=work)
    _run_git("commit", "-q", "-m", "squash: original (#1)", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    # … then add NEW post-merge work on the branch, content absent from origin/main.
    _run_git("checkout", "-q", "feature", cwd=work)
    (work / "post.txt").write_text("post-merge continued\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "feat: continued after merge", cwd=work)
    post_sha = subprocess.run(
        [_GIT, "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()
    _run_git("checkout", "-q", "main", cwd=work)

    with _forge_merged():
        verdict = bc.branch_redundancy(str(work), "feature")
        assert bc.is_squash_merged(str(work), "feature", "main") is False

    assert verdict.redundant is False
    assert verdict.forge_merged is True
    assert verdict.merged_with_post_merge_work is True
    assert post_sha in verdict.unique_shas, f"the post-merge SHA must be emitted: {verdict.unique_shas}"


def test_forge_merged_alone_never_deletes_a_tip_not_on_target(tmp_path: Path) -> None:
    """The #2763 invariant: forge-merged is corroborating-only, never authorises delete."""
    _remote, work = _init(tmp_path)
    _branch_with_commit(work, "feature", "f.txt", "genuinely ahead\n", "feat: ahead")
    _run_git("push", "-q", "origin", "feature", cwd=work)  # pushed, but NOT on origin/main

    with _forge_merged():
        verdict = bc.branch_redundancy(str(work), "feature")
        assert bc.is_squash_merged(str(work), "feature", "main") is False

    assert verdict.redundant is False, "forge-merged alone must NOT make a non-on-target tip redundant"
    assert verdict.forge_merged is True
    assert verdict.merged_with_post_merge_work is True


def test_gone_remote_ref_does_not_abort_detection(tmp_path: Path) -> None:
    """A deleted local branch ref is handled (no rc=128 abort) — fails safe to NOT redundant."""
    _remote, work = _init(tmp_path)
    _branch_with_commit(work, "feature", "f.txt", "work\n", "feat: work")
    _run_git("push", "-q", "origin", "feature", cwd=work)
    _run_git("update-ref", "-d", "refs/heads/feature", cwd=work)  # branch ref gone

    with _no_forge():
        # Must not raise; the content probe fails closed to NOT redundant.
        verdict = bc.branch_redundancy(str(work), "feature")

    assert verdict.redundant is False
    assert verdict.source in {"not-redundant", "inconclusive"}


def test_genuinely_ahead_branch_is_not_redundant(tmp_path: Path) -> None:
    """A never-merged branch with unique content is not redundant under any layer."""
    _remote, work = _init(tmp_path)
    _branch_with_commit(work, "feature", "f.txt", "never merged\n", "feat: never")

    with _no_forge():
        verdict = bc.branch_redundancy(str(work), "feature")

    assert verdict.redundant is False
    assert verdict.source == "not-redundant"
    assert verdict.unique_shas, "the unique commit must be reported for salvage"
