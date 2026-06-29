"""Tests for offline ``origin`` remote-URL resolution (`teatree.hooks.git_config_offline`).

The cwd-remote resolution must work inside the restricted PreToolUse hook
subprocess where a bare ``git`` is unresolvable, so the URL is read by PARSING
``.git/config`` directly -- no subprocess. These tests exercise a real ``git``
checkout under ``tmp_path`` (a main repo AND a linked worktree whose ``.git`` is
a FILE pointing at the shared common-dir config), plus the minimal config
reader on raw config text.
"""

import os
import subprocess
from pathlib import Path

from teatree.hooks import git_config_offline


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


class TestOriginUrlFromCheckout:
    """``origin_url`` resolves the remote from a real ``.git/config`` offline."""

    def test_main_checkout_dir_git(self, tmp_path: Path) -> None:
        # A main checkout's ``.git`` is a directory that is its own common dir.
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:internalcorp/svc.git")
        assert git_config_offline.origin_url(repo) == "git@gitlab.com:internalcorp/svc.git"

    def test_subdirectory_walks_up_to_repo_root(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "r", "https://github.com/acme-internal/app.git")
        sub = repo / "deep" / "nested"
        sub.mkdir(parents=True)
        assert git_config_offline.origin_url(sub) == "https://github.com/acme-internal/app.git"

    def test_linked_worktree_git_file_reads_shared_config(self, tmp_path: Path) -> None:
        # The real-world hook case: the agent's cwd is a LINKED worktree whose
        # ``.git`` is a FILE ``gitdir: ...``; the origin lives in the shared
        # common-dir config and must resolve without a ``git`` subprocess.
        repo = _repo_with_remote(tmp_path / "r", "git@gitlab.com:internalcorp/svc.git")
        _git(repo, "commit", "--allow-empty", "-m", "init")
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", str(linked), "-b", "feat/x")
        assert (linked / ".git").is_file()
        assert git_config_offline.origin_url(linked) == "git@gitlab.com:internalcorp/svc.git"

    def test_non_repo_dir_returns_empty(self, tmp_path: Path) -> None:
        assert git_config_offline.origin_url(tmp_path) == ""

    def test_missing_origin_remote_returns_empty(self, tmp_path: Path) -> None:
        repo = tmp_path / "r"
        repo.mkdir()
        _git(repo, "init", "-b", "main")  # no origin added
        assert git_config_offline.origin_url(repo) == ""


class TestRemoteUrlFromConfig:
    """The minimal git-config reader picks the right ``url`` for ``[remote "..."]``."""

    def test_picks_origin_url_among_sections(self) -> None:
        text = (
            "[core]\n\trepositoryformatversion = 0\n"
            '[remote "upstream"]\n\turl = https://example.com/up/stream.git\n'
            '[remote "origin"]\n\turl = git@gitlab.com:internalcorp/svc.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
        assert git_config_offline._remote_url_from_config(text, "origin") == "git@gitlab.com:internalcorp/svc.git"

    def test_section_name_is_case_insensitive(self) -> None:
        text = '[REMOTE "origin"]\n\tURL = git@gitlab.com:ns/repo.git\n'
        assert git_config_offline._remote_url_from_config(text, "origin") == "git@gitlab.com:ns/repo.git"

    def test_subsection_remote_name_is_case_sensitive(self) -> None:
        # The quoted remote name is case-sensitive in git, so ``Origin`` is a
        # different remote than ``origin`` and must NOT match.
        text = '[remote "Origin"]\n\turl = git@gitlab.com:ns/repo.git\n'
        assert git_config_offline._remote_url_from_config(text, "origin") == ""

    def test_comment_lines_ignored(self) -> None:
        text = '# a comment\n; another\n[remote "origin"]\n\turl = https://github.com/o/r.git\n'
        assert git_config_offline._remote_url_from_config(text, "origin") == "https://github.com/o/r.git"

    def test_absent_remote_returns_empty(self) -> None:
        text = "[core]\n\tbare = false\n"
        assert git_config_offline._remote_url_from_config(text, "origin") == ""
