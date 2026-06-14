"""Tests for the unknown-repo SCOPE push gate in hook_router.

A ``git push`` from a repo whose remote slug is OWNED by a registered overlay
(``owned_repos``) proceeds; a push to an UNKNOWN repo (no overlay claims it)
is denied and routed to the operator-approval path. The gate is OPT-IN — it
fires only because a registered overlay (the always-present t3-teatree
dogfood overlay) declares ``owned_repos`` — and never-lockout: a per-call
``[scope-push-ok: <reason>]`` token and the ``unknown_repo_push_gate_enabled``
kill-switch both ALLOW.

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten remote
so ``slug_for_cwd`` resolves the target slug offline.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


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


def _push_event(command: str, cwd: Path | None) -> dict:
    return {
        "session_id": "sess-scope-push",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd) if cwd is not None else "",
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestAllowsOwnedRepoPush:
    @pytest.mark.parametrize(
        "remote",
        [
            # t3-teatree owns github.com/souliane — these are in scope.
            "git@github.com:souliane/teatree.git",
            "https://github.com/souliane/blog.git",
        ],
    )
    def test_push_to_owned_repo_is_allowed(
        self, remote: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "owned", remote)
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False
        assert _parse_deny(capsys) is None


class TestBlocksUnknownRepoPush:
    @pytest.mark.parametrize(
        "remote",
        [
            "git@github.com:randomuser/randomrepo.git",
            # Same namespace, WRONG host: t3-teatree owns github.com/souliane,
            # not gitlab.com/souliane — the forge-host gate holds this.
            "git@gitlab.com:souliane/teatree.git",
        ],
    )
    def test_push_to_unknown_repo_requires_approval(
        self, remote: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "unknown", remote)
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert deny["permissionDecision"] == "deny"
        assert "owned_repos" in deny["permissionDecisionReason"]

    def test_non_push_command_is_ignored(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unknown2", "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event("git status", repo)) is False

    def test_dry_run_push_is_ignored(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unknown3", "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event("git push --dry-run origin HEAD", repo)) is False


class TestNeverLockout:
    def test_per_call_token_allows(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_with_remote(tmp_path / "tok", "git@github.com:randomuser/randomrepo.git")
        command = "git push origin HEAD  # [scope-push-ok: vetted one-off]"
        assert router.handle_block_unknown_repo_push(_push_event(command, repo)) is False
        assert _parse_deny(capsys) is None

    def test_empty_token_does_not_allow(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_with_remote(tmp_path / "tok2", "git@github.com:randomuser/randomrepo.git")
        command = "git push origin HEAD  # [scope-push-ok: ]"
        assert router.handle_block_unknown_repo_push(_push_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    def test_kill_switch_disables_gate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _repo_with_remote(tmp_path / "killed", "git@github.com:randomuser/randomrepo.git")
        monkeypatch.setattr(router, "_unknown_repo_push_gate_enabled", lambda: False)
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False

    def test_unresolvable_cwd_fails_open(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", None)) is False
        assert _parse_deny(capsys) is None
