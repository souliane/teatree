# test-path: cross-cutting — drives the hooks/scripts/glab_stale_base_remote_guard.py
# PreToolUse gate; the teatree.config import only reads the cold-hook settings that gate
# consults, so there is no src/teatree/ module this file mirrors.
"""Tests for the stale ``glab-base`` remote PreToolUse gate.

``glab`` persists a ``-R <slug>`` override as a git remote named ``glab-base`` in
the cwd repository. A LATER ``glab mr create -R <other-slug>`` from that same
directory then hits a stale override and glab bails: exit 0, no output, no MR.
Measured against glab 1.80.4 — renaming the remote is the only difference
between that silence and a loud GitLab rejection.

The tests build real git repositories under ``tmp_path`` (no network, no glab)
because the whole behaviour under test is "what do this directory's remotes
say", and a mocked ``git`` would pin the mock rather than the resolution.
"""

import subprocess
from pathlib import Path

import pytest

import hooks.scripts.glab_stale_base_remote_guard as guard
from hooks.scripts.glab_stale_base_remote_guard import (
    BASE_REMOTE,
    command_working_dir,
    handle_block_glab_stale_base_remote,
    project_path,
    stale_base_remote,
)
from teatree.config import COLD_HOOK_SETTINGS
from tests._git_repo import _GIT

_TARGET = "acme-engineering/widget-translations"
_OTHER_URL = "git@gitlab.com:acme-engineering/platform/other-project.git"

_CREATE = (
    f"glab mr create -R {_TARGET} --source-branch 4242-feature-x "
    "--title 'feat(x): a real title (proj#1)' --description \"$(< body.md)\" --yes"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with an ``origin`` remote and no ``glab-base``."""
    work = tmp_path / "clone"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "remote", "add", "origin", f"git@gitlab.com:{_TARGET}.git")
    return work


def _payload(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


class TestProjectPathNormalisation:
    """A ``-R`` slug and a git remote URL must reduce to one comparable path."""

    @pytest.mark.parametrize(
        "value",
        [
            "acme-engineering/widget-translations",
            "git@gitlab.com:acme-engineering/widget-translations.git",
            "https://gitlab.com/acme-engineering/widget-translations.git",
            "https://gitlab.com/acme-engineering/widget-translations",
            "ACME-Engineering/Widget-Translations",
        ],
    )
    def test_every_spelling_of_one_project_compares_equal(self, value: str):
        assert project_path(value) == _TARGET

    def test_subgroup_path_is_preserved(self):
        assert project_path(_OTHER_URL) == "acme-engineering/platform/other-project"


class TestStaleRemoteDetection:
    """Only a ``glab-base`` pointing at a DIFFERENT project is stale."""

    def test_no_glab_base_remote_is_not_stale(self, repo: Path):
        assert stale_base_remote(repo, _TARGET) is None

    def test_matching_glab_base_remote_is_not_stale(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, f"git@gitlab.com:{_TARGET}.git")
        assert stale_base_remote(repo, _TARGET) is None

    def test_mismatched_glab_base_remote_is_stale(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        assert stale_base_remote(repo, _TARGET) == _OTHER_URL

    def test_non_git_directory_is_not_stale(self, tmp_path: Path):
        assert stale_base_remote(tmp_path, _TARGET) is None


class TestWorkingDirResolution:
    """glab reads the remotes of the directory it RUNS in, ``cd`` included."""

    def test_leading_cd_wins_over_the_harness_cwd(self, repo: Path, tmp_path: Path):
        assert command_working_dir(f"cd {repo} && glab mr create -R {_TARGET}", str(tmp_path)) == repo

    def test_quoted_leading_cd_is_resolved(self, repo: Path, tmp_path: Path):
        assert command_working_dir(f"cd '{repo}' && glab mr create -R {_TARGET}", str(tmp_path)) == repo

    def test_bare_command_uses_the_harness_cwd(self, repo: Path):
        assert command_working_dir(f"glab mr create -R {_TARGET}", str(repo)) == repo

    def test_unresolvable_directory_yields_none(self, tmp_path: Path):
        assert command_working_dir("glab mr create -R x/y", str(tmp_path / "gone")) is None


class TestGateVerdict:
    """The block replaces glab's silent no-op; everything else is allowed."""

    def test_create_under_a_stale_base_remote_is_denied_with_a_named_reason(self, repo: Path, capsys):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        assert handle_block_glab_stale_base_remote(_payload(_CREATE, repo)) is True
        reason = capsys.readouterr().out
        assert BASE_REMOTE in reason
        assert "other-project" in reason
        assert _TARGET in reason

    def test_create_from_the_target_repo_clone_is_allowed(self, repo: Path, capsys):
        assert handle_block_glab_stale_base_remote(_payload(_CREATE, repo)) is False
        assert capsys.readouterr().out == ""

    def test_matching_base_remote_is_allowed(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, f"git@gitlab.com:{_TARGET}.git")
        assert handle_block_glab_stale_base_remote(_payload(_CREATE, repo)) is False

    def test_a_read_command_is_never_blocked(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        command = f"glab mr list -R {_TARGET} --source-branch 4242-feature-x"
        assert handle_block_glab_stale_base_remote(_payload(command, repo)) is False

    def test_create_without_an_explicit_target_is_allowed(self, repo: Path):
        # No ``-R``: glab resolves the repo from the cwd, which is the shape the
        # stale override does not break. Blocking it would be a guess.
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        command = "glab mr create --title 'feat(x): a real title (proj#1)' --description 'body'"
        assert handle_block_glab_stale_base_remote(_payload(command, repo)) is False

    def test_the_phrase_inside_a_commit_message_does_not_fire(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        command = f"git commit -m 'docs: explain why glab mr create -R {_TARGET} went silent'"
        assert handle_block_glab_stale_base_remote(_payload(command, repo)) is False

    def test_a_non_bash_tool_is_ignored(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        data = {"tool_name": "Edit", "cwd": str(repo), "tool_input": {"file_path": "x.py"}}
        assert handle_block_glab_stale_base_remote(data) is False


class TestNeverLockout:
    """A gate that replaces a silent block must not become a new hard block."""

    def test_per_call_token_allows_with_a_note(self, repo: Path, capsys):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        command = f"{_CREATE} [glab-base-ok: verified this reaches GitLab]"
        assert handle_block_glab_stale_base_remote(_payload(command, repo)) is False
        assert "glab-base-ok" in capsys.readouterr().err

    def test_empty_token_reason_does_not_allow(self, repo: Path):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        assert handle_block_glab_stale_base_remote(_payload(f"{_CREATE} [glab-base-ok: ]", repo)) is True

    def test_kill_switch_disables_the_gate(self, repo: Path, monkeypatch):
        _git(repo, "remote", "add", BASE_REMOTE, _OTHER_URL)
        monkeypatch.setattr(guard, "_gate_enabled", lambda: False)
        assert handle_block_glab_stale_base_remote(_payload(_CREATE, repo)) is False

    def test_the_kill_switch_key_is_a_registered_cold_hook_setting(self):
        assert COLD_HOOK_SETTINGS["glab_stale_base_remote_gate_enabled"].default is True
