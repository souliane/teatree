# test-path: cross-cutting — tests hook_router.py (hooks/), which has no single src/teatree/ mirror.
"""Tests for the unknown-repo SCOPE push gate in hook_router.

A ``git push`` from a repo whose remote slug is OWNED by a registered overlay
(``owned_repos``) proceeds; a push to an UNKNOWN repo (no overlay claims it)
is denied and routed to the operator-approval path. The gate is OPT-IN and
ships INERT (``require_owned_repo_approval`` defaults False), so these tests
inject an opted-in overlay set rather than relying on the shipped flag —
the gate LOGIC is what they assert, not the dogfood overlay's config. It is
never-lockout: a per-call ``[scope-push-ok: <reason>]`` token and the
``unknown_repo_push_gate_enabled`` kill-switch both ALLOW.

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten remote
so ``slug_for_cwd`` resolves the target slug offline.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.unknown_repo_push_gate as scope_gate
from teatree.core.overlay import OverlayBase, OverlayConfig

if TYPE_CHECKING:
    from teatree.core.models.worktree import Worktree
    from teatree.core.overlay import ProvisionStep


class _OptedInOverlay(OverlayBase):
    def __init__(self) -> None:
        self.config = OverlayConfig()
        self.config.owned_repos = {"github.com": ["souliane"]}
        self.config.require_owned_repo_approval = True

    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: "Worktree") -> list["ProvisionStep"]:
        _ = worktree
        return []


@pytest.fixture(autouse=True)
def _opt_in_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gate's overlay set an opted-in t3-teatree (the shipped overlay is inert)."""
    monkeypatch.setattr(
        "teatree.core.overlay_loader.get_all_overlays",
        lambda: {"t3-teatree": _OptedInOverlay()},
    )


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

    def test_git_dash_c_push_is_classified_by_the_named_repo(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``git -C <dir> push`` names the repo; the ambient cwd does not."""
        owned = _repo_with_remote(tmp_path / "session", "git@github.com:souliane/teatree.git")
        unknown = _repo_with_remote(tmp_path / "elsewhere", "git@github.com:randomuser/randomrepo.git")
        event = _push_event(f"git -C {unknown} push origin main", owned)
        assert router.handle_block_unknown_repo_push(event) is True
        assert _parse_deny(capsys) is not None

    def test_cd_prefixed_push_is_classified_by_the_cd_target(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        owned = _repo_with_remote(tmp_path / "session2", "git@github.com:souliane/teatree.git")
        unknown = _repo_with_remote(tmp_path / "elsewhere2", "git@github.com:randomuser/randomrepo.git")
        event = _push_event(f"cd {unknown} && git push origin main", owned)
        assert router.handle_block_unknown_repo_push(event) is True
        assert _parse_deny(capsys) is not None

    def test_dash_c_push_to_an_owned_repo_is_allowed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The other direction: the named repo is owned even though the session sits elsewhere."""
        unknown = _repo_with_remote(tmp_path / "session3", "git@github.com:randomuser/randomrepo.git")
        owned = _repo_with_remote(tmp_path / "elsewhere3", "git@github.com:souliane/teatree.git")
        event = _push_event(f"git -C {owned} push origin main", unknown)
        assert router.handle_block_unknown_repo_push(event) is False
        assert _parse_deny(capsys) is None

    def test_push_inside_a_quoted_string_is_not_a_push(self, tmp_path: Path) -> None:
        """The verb is read off the quote-stripped skeleton, as in every sibling gate."""
        repo = _repo_with_remote(tmp_path / "quoted", "git@github.com:randomuser/randomrepo.git")
        event = _push_event("git commit -m 'do not git push this yet'", repo)
        assert router.handle_block_unknown_repo_push(event) is False

    def test_non_push_command_is_ignored(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unknown2", "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event("git status", repo)) is False

    def test_dry_run_push_is_ignored(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unknown3", "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event("git push --dry-run origin HEAD", repo)) is False

    def test_dash_c_dry_run_push_is_ignored(self, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / "unknown4", "git@github.com:randomuser/randomrepo.git")
        event = _push_event(f"git -C {repo} push --dry-run origin HEAD", repo)
        assert router.handle_block_unknown_repo_push(event) is False


class TestDegradedPathIsAudible:
    """An allow the gate could not actually evaluate must not read like a cleared push."""

    def test_missing_django_says_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "nodjango", "git@github.com:randomuser/randomrepo.git")
        monkeypatch.setattr(scope_gate, "bootstrap_teatree_django", lambda: False)

        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False

        assert "SKIPPED is not PASSED" in capsys.readouterr().err

    def test_broken_resolver_says_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "brokenresolver", "git@github.com:randomuser/randomrepo.git")

        registry_down = RuntimeError("overlay registry unavailable")

        def _boom(_cwd: Path) -> str:
            raise registry_down

        monkeypatch.setattr("teatree.core.gates.owned_repo_guard.classify_active_push", _boom)

        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False

        assert "SKIPPED is not PASSED" in capsys.readouterr().err

    def test_a_cleared_push_stays_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_with_remote(tmp_path / "cleared", "git@github.com:souliane/teatree.git")

        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False

        assert "SKIPPED is not PASSED" not in capsys.readouterr().err


class TestTheDryRunExemptionBelongsToItsOwnPush:
    """The exemption is read off ONE push's own arguments, not the whole command.

    A whole-command search hands one push's ``--dry-run`` to every push chained
    beside it, so ``git push --dry-run x && git push y`` walks the live half
    straight past the scope gate — in EITHER order, and under any separator. The
    same search also exempted a real push on the strength of the words merely
    appearing in a quoted commit message or another option's value.
    """

    @pytest.mark.parametrize(
        ("case_id", "command"),
        [
            ("quoted-in-commit-message", "git commit -m 'run git push --dry-run first' && git push origin HEAD"),
            ("inside-an-option-value", "git push origin HEAD -o merge_request.title='ship the --dry-run mode'"),
            ("dry-run-leads", "git push --dry-run origin HEAD && git push origin HEAD"),
            ("dry-run-trails", "git push origin HEAD && git push --dry-run origin HEAD"),
            ("semicolon-separated", "git push -n origin HEAD ; git push origin HEAD"),
        ],
    )
    def test_a_real_push_beside_a_dry_run_mention_is_still_gated(
        self, case_id: str, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / case_id, "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    def test_a_dash_c_dry_run_does_not_exempt_the_dash_c_push_chained_after_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The prefix that lets `git -C <dir> push` be seen at all consumes the leading
        # separator, so the per-push segment must start at the verb, not at the match.
        repo = _repo_with_remote(tmp_path / "dash-c-chain", "git@github.com:randomuser/randomrepo.git")
        command = f"git -C {repo} push --dry-run origin HEAD && git -C {repo} push origin HEAD"
        assert router.handle_block_unknown_repo_push(_push_event(command, repo)) is True
        assert _parse_deny(capsys) is not None

    @pytest.mark.parametrize("command", ["git push -n origin HEAD", "git push --dry-run"])
    def test_a_genuine_dry_run_is_still_exempt(self, command: str, tmp_path: Path) -> None:
        repo = _repo_with_remote(tmp_path / command[-6:].replace(" ", "_"), "git@github.com:randomuser/randomrepo.git")
        assert router.handle_block_unknown_repo_push(_push_event(command, repo)) is False


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
        monkeypatch.setattr(scope_gate, "_unknown_repo_push_gate_enabled", lambda: False)
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", repo)) is False

    def test_unresolvable_cwd_fails_open(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_unknown_repo_push(_push_event("git push origin HEAD", None)) is False
        assert _parse_deny(capsys) is None
