import os
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from teatree.utils import git
from teatree.utils import run as utils_run_mod


class TestUnsyncedCommits:
    def test_returns_empty_list_when_fully_synced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 0, stdout="", stderr=""),
        )
        assert git.unsynced_commits("/repo", "feature") == []

    def test_returns_commit_lines_when_commits_exist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "abc123 chore: cve fix\ndef456 feat: add something\n"
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 0, stdout=output, stderr=""),
        )
        result = git.unsynced_commits("/repo", "feature")
        assert result == ["abc123 chore: cve fix", "def456 feat: add something"]

    def test_filters_blank_lines_from_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "abc123 fix something\n\n   \ndef456 another fix\n"
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 0, stdout=output, stderr=""),
        )
        result = git.unsynced_commits("/repo", "feature")
        assert result == ["abc123 fix something", "def456 another fix"]

    def test_pushed_branch_with_commit_not_on_main_is_ahead(self, tmp_path: Path) -> None:
        """A commit pushed to its own remote branch but not on main must still be ahead.

        Regression: ``git log --not --remotes`` excludes ALL remote tracking
        refs, so a pushed feature branch's own commits get filtered out and
        the classifier mis-reports the branch as synced. The unsynced check
        must compare against the default branch, not against every remote.
        """
        bare = tmp_path / "remote.git"
        utils_run_mod.run_checked(["git", "init", "--bare", str(bare)])
        local = tmp_path / "local"
        utils_run_mod.run_checked(["git", "clone", str(bare), str(local)])
        for k, v in {"user.email": "t@x", "user.name": "t", "commit.gpgsign": "false"}.items():
            utils_run_mod.run_checked(["git", "-C", str(local), "config", k, v])
        (local / "a").write_text("1\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "a"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "main commit"])
        utils_run_mod.run_checked(["git", "-C", str(local), "branch", "-M", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "checkout", "-b", "feature"])
        (local / "b").write_text("2\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "b"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "feature work"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "feature"])

        result = git.unsynced_commits(str(local), "feature")
        assert len(result) == 1
        assert "feature work" in result[0]


class TestCommitsAbsentFromAllRemotes:
    """#706 data-loss guard helper — commits reachable from no remote ref."""

    def test_returns_empty_when_nothing_unpushed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 0, stdout="", stderr=""),
        )
        assert git.commits_absent_from_all_remotes("/repo", "feature") == []

    def test_returns_lines_and_filters_blanks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = "abc123 unpushed work\n\n   \ndef456 more local\n"
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 0, stdout=output, stderr=""),
        )
        assert git.commits_absent_from_all_remotes("/repo", "feature") == [
            "abc123 unpushed work",
            "def456 more local",
        ]

    def test_pushed_branch_not_on_main_is_considered_safe(self, tmp_path: Path) -> None:
        """A pushed-but-unmerged branch has nothing absent from all remotes.

        The work survives on its own remote ref, so teardown is safe. This is
        the inverse of ``unsynced_commits`` and the reason #706 needs a
        distinct helper.
        """
        bare = tmp_path / "remote.git"
        utils_run_mod.run_checked(["git", "init", "--bare", str(bare)])
        local = tmp_path / "local"
        utils_run_mod.run_checked(["git", "clone", str(bare), str(local)])
        for k, v in {"user.email": "t@x", "user.name": "t", "commit.gpgsign": "false"}.items():
            utils_run_mod.run_checked(["git", "-C", str(local), "config", k, v])
        (local / "a").write_text("1\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "a"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "main commit"])
        utils_run_mod.run_checked(["git", "-C", str(local), "branch", "-M", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "checkout", "-b", "feature"])
        (local / "b").write_text("2\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "b"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "feature work"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "feature"])

        # Pushed → reachable from refs/remotes/origin/feature → nothing absent.
        assert git.commits_absent_from_all_remotes(str(local), "feature") == []

    def test_local_only_commit_is_flagged(self, tmp_path: Path) -> None:
        """A commit that was never pushed anywhere is reported (data loss risk)."""
        bare = tmp_path / "remote.git"
        utils_run_mod.run_checked(["git", "init", "--bare", str(bare)])
        local = tmp_path / "local"
        utils_run_mod.run_checked(["git", "clone", str(bare), str(local)])
        for k, v in {"user.email": "t@x", "user.name": "t", "commit.gpgsign": "false"}.items():
            utils_run_mod.run_checked(["git", "-C", str(local), "config", k, v])
        (local / "a").write_text("1\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "a"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "main commit"])
        utils_run_mod.run_checked(["git", "-C", str(local), "branch", "-M", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "checkout", "-b", "feature"])
        (local / "b").write_text("2\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "b"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "never pushed"])

        result = git.commits_absent_from_all_remotes(str(local), "feature")
        assert len(result) == 1
        assert "never pushed" in result[0]

    def test_git_error_raises_instead_of_returning_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#706 fail-closed on a non-zero git exit.

        A git error must NOT be read as "nothing unpushed" — it raises so the
        data-loss guard refuses teardown.
        """
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *_a, **_k: CompletedProcess([], 128, stdout="", stderr="fatal: bad revision"),
        )
        with pytest.raises(utils_run_mod.CommandFailedError):
            git.commits_absent_from_all_remotes("/repo", "feature")

    def test_missing_branch_raises_not_silently_empty(self, tmp_path: Path) -> None:
        """An unknown branch makes ``git log`` exit 128 → must raise, not return []."""
        bare = tmp_path / "remote.git"
        utils_run_mod.run_checked(["git", "init", "--bare", str(bare)])
        local = tmp_path / "local"
        utils_run_mod.run_checked(["git", "clone", str(bare), str(local)])
        for k, v in {"user.email": "t@x", "user.name": "t", "commit.gpgsign": "false"}.items():
            utils_run_mod.run_checked(["git", "-C", str(local), "config", k, v])
        (local / "a").write_text("1\n")
        utils_run_mod.run_checked(["git", "-C", str(local), "add", "a"])
        utils_run_mod.run_checked(["git", "-C", str(local), "commit", "-m", "main commit"])
        utils_run_mod.run_checked(["git", "-C", str(local), "branch", "-M", "main"])
        utils_run_mod.run_checked(["git", "-C", str(local), "push", "origin", "main"])

        with pytest.raises(utils_run_mod.CommandFailedError):
            git.commits_absent_from_all_remotes(str(local), "does-not-exist")


def _init_dirty_repo(path: Path, *, marker: str = "tracked") -> None:
    """A repo with one committed file edited dirty + one untracked file.

    ``marker`` distinguishes two repos by filename so a hijacked ``git`` call
    produces a visibly-wrong patch (the decoy's filenames, not the target's).
    """
    path.mkdir()
    utils_run_mod.run_checked(["git", "init", "-q", "-b", "main", str(path)])
    for k, v in {"user.email": "t@x", "user.name": "t", "commit.gpgsign": "false"}.items():
        utils_run_mod.run_checked(["git", "-C", str(path), "config", k, v])
    (path / f"{marker}.txt").write_text("base\n", encoding="utf-8")
    utils_run_mod.run_checked(["git", "-C", str(path), "add", "-A"])
    utils_run_mod.run_checked(["git", "-C", str(path), "commit", "-q", "-m", "initial"])
    (path / f"{marker}.txt").write_text("base\nEDITED\n", encoding="utf-8")
    (path / f"{marker}_new.txt").write_text("new content\n", encoding="utf-8")


class TestFullWorktreeDiff:
    """#835 — the captured patch must be plain-``git apply``-able everywhere."""

    def test_patch_has_standard_prefixes_under_diff_noprefix_config(self, tmp_path: Path) -> None:
        """Repo-local ``diff.noprefix=true`` must NOT strip ``a/``/``b/`` prefixes.

        ``git diff`` honours the caller's git config; a user with
        ``diff.noprefix=true`` (common; was set on the review machine) would
        otherwise get a prefix-less patch that a plain ``git apply`` cannot
        restore — total loss of the captured work, the exact #835 scenario.
        The config is set REPO-LOCAL on purpose: the conftest HOME sandbox
        masks real user git config, so a HOME-scoped setting would not exercise
        this. RED on ``git diff HEAD --binary`` (no prefix flags); GREEN once
        ``--src-prefix=a/ --dst-prefix=b/`` is forced.
        """
        repo = tmp_path / "repo"
        _init_dirty_repo(repo)
        utils_run_mod.run_checked(["git", "-C", str(repo), "config", "diff.noprefix", "true"])

        patch_text = git.full_worktree_diff(str(repo))

        assert "--- a/tracked.txt" in patch_text
        assert "+++ b/tracked.txt" in patch_text

        restore = tmp_path / "restore"
        utils_run_mod.run_checked(["git", "init", "-q", "-b", "main", str(restore)])
        (restore / "tracked.txt").write_text("base\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        diff_file = tmp_path / "wt.diff"
        diff_file.write_text(patch_text, encoding="utf-8")
        # Plain ``git apply`` — the restore contract the docstring/BLUEPRINT promise.
        utils_run_mod.run_checked(["git", "-C", str(restore), "apply", str(diff_file)], env=env)
        assert (restore / "tracked.txt").read_text(encoding="utf-8") == "base\nEDITED\n"
        assert (restore / "tracked_new.txt").read_text(encoding="utf-8") == "new content\n"

    def test_ignores_poisoned_git_env_overrides(self, tmp_path: Path) -> None:
        """A hijacking ``GIT_*`` env must not redirect the diff to another repo.

        The inline pre-commit ``pytest`` hook runs under an outer ``git commit``
        exporting ``GIT_DIR``/``GIT_WORK_TREE``. Inherited, they would point the
        capture's ``git`` at the outer repo instead of the worktree it was
        asked about. RED if the ``GIT_*`` strip is reverted.
        """
        target = tmp_path / "target"
        _init_dirty_repo(target, marker="target")
        decoy = tmp_path / "decoy"
        _init_dirty_repo(decoy, marker="decoy")

        with patch.dict(
            os.environ,
            {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)},
        ):
            patch_text = git.full_worktree_diff(str(target))

        # The patch must describe the TARGET's files. A leaked GIT_* would
        # redirect the diff to the decoy repo — its filenames would appear and
        # the target's would not.
        assert "a/target.txt" in patch_text, patch_text
        assert "target_new.txt" in patch_text, patch_text
        assert "decoy" not in patch_text, patch_text
