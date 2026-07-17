"""Real (non-regenerable) uncommitted-change detection — the shared dirt probe.

:func:`real_uncommitted_reasons` backs both the dirty-worktree teardown guard and
the analyze-before-wipe done pass, so both decide "does this worktree hold real
uncommitted work?" identically. It ignores the regenerable env cache and the
"every tracked file reads as a staged add" noise of a dangling-HEAD (post-merge
branch-ref deletion) worktree, and fails CLOSED on an inconclusive probe.

Happy paths run against real git under ``tmp_path``; the fail-closed error
branches inject a ``CommandFailedError`` from the (unstoppable) git subprocess.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.cleanup_orphan_ref import OrphanRefDecision
from teatree.core.cleanup.working_tree_dirt import real_uncommitted_reasons
from teatree.utils import git
from teatree.utils.run import CommandFailedError
from tests.teatree_core.cleanup._shared import _GIT, _run_git


def _corrupt_index(wt_dir: Path) -> None:
    """Corrupt the real on-disk index for a worktree so ``git status`` itself fails.

    A ``git worktree add`` checkout's ``.git`` is a *file* (a gitdir pointer),
    not a directory, and each worktree has its own per-worktree index living
    under the main repo's ``.git/worktrees/<name>/index`` — not
    ``<wt_dir>/.git/index``. Resolve the real git-dir via ``rev-parse`` first.
    """
    result = subprocess.run(
        [_GIT, "-C", str(wt_dir), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = wt_dir / git_dir
    (git_dir / "index").write_bytes(b"not a real git index")


def _committed_worktree(tmp_path: Path) -> tuple[Path, _EffectiveTarget]:
    """A real worktree on ``feat`` with one committed file and a resolvable HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "t@t", cwd=repo)
    _run_git("config", "user.name", "t", cwd=repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _run_git("add", "-A", cwd=repo)
    _run_git("commit", "-q", "-m", "initial", cwd=repo)

    wt_dir = tmp_path / "wt"
    _run_git("worktree", "add", "-q", "-b", "feat", str(wt_dir), cwd=repo)
    target = _EffectiveTarget(ref="HEAD", probe_repo=str(wt_dir), branch_to_delete="feat", label="feat")
    return wt_dir, target


def _dangling_head_worktree(tmp_path: Path) -> tuple[Path, _EffectiveTarget]:
    """A real worktree whose branch ref was deleted — HEAD is a dangling symref."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=repo)
    _run_git("config", "user.email", "t@t", cwd=repo)
    _run_git("config", "user.name", "t", cwd=repo)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _run_git("add", "-A", cwd=repo)
    _run_git("commit", "-q", "-m", "initial", cwd=repo)

    wt_dir = tmp_path / "wt"
    _run_git("worktree", "add", "-q", "-b", "feat", str(wt_dir), cwd=repo)
    _run_git("config", "user.email", "t@t", cwd=wt_dir)
    _run_git("config", "user.name", "t", cwd=wt_dir)
    (wt_dir / "feat.py").write_text("y = 1\n", encoding="utf-8")
    _run_git("add", "-A", cwd=wt_dir)
    _run_git("commit", "-q", "-m", "feat work", cwd=wt_dir)
    _run_git("update-ref", "-d", "refs/heads/feat", cwd=repo)

    target = _EffectiveTarget(ref=git.DETACHED_HEAD, probe_repo=str(wt_dir), branch_to_delete=None, label="feat")
    return wt_dir, target


class TestRealUncommittedReasons:
    """The dirt probe: clean vs dirty, regenerable-artifact + dangling-HEAD handling, fail-closed."""

    def test_missing_dir_is_clean(self, tmp_path: Path) -> None:
        target = _EffectiveTarget(ref="HEAD", probe_repo="/nope", branch_to_delete="feat", label="feat")
        assert real_uncommitted_reasons(str(tmp_path / "gone"), target) == []

    def test_clean_worktree_has_no_reasons(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        assert real_uncommitted_reasons(str(wt_dir), target) == []

    def test_real_tracked_edit_is_dirty(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        (wt_dir / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert len(reasons) == 1
        assert "tracked.py" in reasons[0]

    def test_regenerable_env_cache_is_ignored(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        (wt_dir / ".t3-env.cache").write_text("DB=x\n", encoding="utf-8")
        assert real_uncommitted_reasons(str(wt_dir), target) == []

    def test_status_read_error_fails_closed(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        boom = CommandFailedError(["git", "status"], 1, "", "index locked")
        with (
            patch("teatree.core.cleanup.working_tree_dirt.git.check", return_value=True),
            patch("teatree.core.cleanup.working_tree_dirt.git.status_porcelain_strict", side_effect=boom),
        ):
            reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons == [f"could not read working-tree status ({boom}) — keeping"]

    def test_blank_porcelain_line_is_skipped(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        with (
            patch("teatree.core.cleanup.working_tree_dirt.git.check", return_value=True),
            patch("teatree.core.cleanup.working_tree_dirt.git.status_porcelain_strict", return_value="\n M real.py"),
        ):
            reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert len(reasons) == 1
        assert "real.py" in reasons[0]

    def test_real_corrupt_index_fails_closed_not_clean(self, tmp_path: Path) -> None:
        """A REAL (unmocked) git failure must be treated as dirty, not clean.

        Regression for a bug where the probe called the lenient
        :func:`teatree.utils.git.status_porcelain`, which swallows a non-zero
        ``git status`` exit and returns whatever (possibly empty) stdout it got —
        so a genuine read failure (corrupt index, lock contention) was
        indistinguishable from a clean tree, and the reaper could wipe a
        worktree it could not actually prove was safe to wipe. This test
        corrupts the on-disk index directly (no mocking) so the underlying
        ``git status --porcelain`` subprocess itself fails, the way it would
        under real lock contention or disk corruption.
        """
        wt_dir, target = _committed_worktree(tmp_path)
        _corrupt_index(wt_dir)
        reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons != []
        assert "keeping" in reasons[0]

    def test_truncates_preview_beyond_limit(self, tmp_path: Path) -> None:
        wt_dir, target = _committed_worktree(tmp_path)
        for name in ("a.py", "b.py", "c.py", "d.py"):
            (wt_dir / name).write_text("z = 1\n", encoding="utf-8")
        reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons[0].startswith("4 uncommitted change(s)")
        assert reasons[0].endswith("…")


class TestDanglingHeadDirtReasons:
    """The post-merge-orphan path: HEAD unresolvable, diffed against the recovered SHA."""

    def test_clean_dangling_head_is_not_dirty(self, tmp_path: Path) -> None:
        wt_dir, target = _dangling_head_worktree(tmp_path)
        assert real_uncommitted_reasons(str(wt_dir), target) == []

    def test_dirty_dangling_head_is_dirty(self, tmp_path: Path) -> None:
        wt_dir, target = _dangling_head_worktree(tmp_path)
        (wt_dir / "feat.py").write_text("y = 999\n", encoding="utf-8")
        reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert len(reasons) == 1
        assert "feat.py" in reasons[0]

    def test_unrecoverable_head_fails_closed(self, tmp_path: Path) -> None:
        wt_dir, target = _dangling_head_worktree(tmp_path)
        undecided = OrphanRefDecision(recovered_sha=None, in_remote=False, unsynced=[])
        with patch("teatree.core.cleanup.working_tree_dirt.classify_orphan_ref", return_value=undecided):
            reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons == ["could not recover HEAD to check working-tree changes — keeping"]

    def test_diff_error_fails_closed(self, tmp_path: Path) -> None:
        wt_dir, target = _dangling_head_worktree(tmp_path)
        recovered = OrphanRefDecision(recovered_sha="abc1234", in_remote=False, unsynced=[])
        boom = CommandFailedError(["git", "diff"], 128, "", "bad revision")
        with (
            patch("teatree.core.cleanup.working_tree_dirt.classify_orphan_ref", return_value=recovered),
            patch("teatree.core.cleanup.working_tree_dirt.git.check", return_value=False),
            patch("teatree.core.cleanup.working_tree_dirt.git.run_strict", side_effect=boom) as mock_run,
        ):
            reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons == [f"could not diff working tree against recovered HEAD ({boom}) — keeping"]
        assert mock_run.called

    def test_real_corrupt_index_fails_closed_for_dangling_head(self, tmp_path: Path) -> None:
        """A REAL (unmocked) diff/ls-files failure on the dangling-HEAD path must fail closed too.

        Same regression as the resolvable-HEAD case above, for the sibling
        lenient calls (``git.run`` for ``diff``/``ls-files``) that also needed
        to move to :func:`teatree.utils.git.run_strict`.
        """
        wt_dir, target = _dangling_head_worktree(tmp_path)
        _corrupt_index(wt_dir)
        reasons = real_uncommitted_reasons(str(wt_dir), target)
        assert reasons != []
        assert "keeping" in reasons[0]
