"""Tests for the cwd-aware out-of-band merge gate in hook_router (#126).

``gh pr merge`` / ``glab mr merge`` must stay BLOCKED for a teatree-managed
repo (it must use the keystone ``t3 <overlay> ticket merge`` transition) but
be ALLOWED in a lightweight repo that has no ticket/overlay FSM — the old
static-regex block hard-denied every repo, a permanent lockout. The gate is
fail-safe: a cwd or slug it cannot resolve is treated as managed and BLOCKED.

Tests use a real ``git init`` repo under ``tmp_path`` with a rewritten remote
plus a tmp ``~/.teatree.toml`` so the managed-repo signals resolve offline.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router


class _FakeHomePath:
    """Drop-in for ``router.Path`` pinning ``home()`` to a tmp dir.

    Patches only the router module's ``Path`` reference, not
    ``pathlib.Path.home`` globally (which would break pytest's tmp machinery).
    """

    def __init__(self, home: Path) -> None:
        self._home = home

    def __call__(self, *args: object, **kwargs: object) -> Path:
        return Path(*args, **kwargs)

    def home(self) -> Path:
        return self._home


def _patch_home(home: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir(exist_ok=True)
    (home / ".teatree.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(router, "Path", _FakeHomePath(home))


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


def _merge_event(command: str, cwd: Path | None) -> dict:
    return {
        "session_id": "sess-merge",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd) if cwd is not None else "",
    }


def _parse_deny(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


_MANAGED_CONFIG = """
[overlays.example]
workspace_repos = ["example-org/private-repo"]
"""


class TestBlocksManagedRepoMerge:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr merge 7 --repo example-org/private-repo --squash",
            "glab mr merge !12 --squash",
        ],
    )
    def test_managed_repo_merge_is_blocked(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "ticket merge" in deny["permissionDecisionReason"]

    def test_teatree_core_repo_merge_is_blocked(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 1", repo)) is True
        assert _parse_deny(capsys) is not None


class TestAllowsUnmanagedRepoMerge:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr merge 3 --squash",
            "glab mr merge !4",
        ],
    )
    def test_unmanaged_repo_merge_is_allowed(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A repo no overlay claims (and not teatree core) has no keystone path.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""


class TestFailsSafeOnUncertainty:
    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    def test_missing_cwd_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 1", None)) is True
        assert _parse_deny(capsys) is not None

    def test_repo_without_remote_is_blocked(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # No origin → slug cannot resolve → uncertain → BLOCK (never weaken).
        repo = tmp_path / "wt"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        assert router.handle_block_out_of_band_merge(_merge_event("glab mr merge !9", repo)) is True
        assert _parse_deny(capsys) is not None

    def test_non_merge_command_passes_through(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr view 3", repo)) is False
        assert capsys.readouterr().out.strip() == ""


# ── REST-API merge-endpoint bypass tests ─────────────────────────────────────
#
# ``gh api repos/OWNER/REPO/pulls/<n>/merge -X PUT`` and the GitLab equivalent
# are semantically identical to ``gh pr merge`` but bypass the literal-subcommand
# regex entirely. The extended gate must deny these on managed repos with the same
# cwd-aware logic, and must allow GET reads of the merge-status endpoint.


class TestBlocksApiMergeEndpointOnManagedRepo:
    """REST-API writes to the merge endpoint are denied on managed repos."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    @pytest.mark.parametrize(
        "command",
        [
            # GitHub PUT (the standard merge method).
            "gh api repos/example-org/private-repo/pulls/12/merge -X PUT",
            # GitLab POST.
            "glab api projects/5/merge_requests/9/merge --method POST",
            # PATCH is also a write.
            "gh api repos/example-org/private-repo/pulls/12/merge --method PATCH",
            # Last-wins: earlier GET overridden by trailing PUT → write.
            "gh api repos/example-org/private-repo/pulls/12/merge -X GET -X PUT",
        ],
    )
    def test_api_merge_write_is_denied_on_managed_repo(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@gitlab.com:example-org/private-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "ticket merge" in deny["permissionDecision" + "Reason"]

    def test_api_merge_write_missing_cwd_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        command = "gh api repos/example-org/private-repo/pulls/12/merge -X PUT"
        assert router.handle_block_out_of_band_merge(_merge_event(command, None)) is True
        assert _parse_deny(capsys) is not None


class TestAllowsApiMergeEndpointReadsAndUnrelated:
    """GET reads of the merge endpoint and unrelated commands pass through."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    @pytest.mark.parametrize(
        "command",
        [
            # Bare GET (no method flag, no body flag) — reads merge status.
            "gh api repos/example-org/private-repo/pulls/12/merge",
            # Explicit GET — also a read.
            "gh api repos/example-org/private-repo/pulls/12/merge -X GET",
            # Last-wins: trailing GET overrides earlier PUT → read.
            "gh api repos/example-org/private-repo/pulls/12/merge -X PUT -X GET",
            # Unrelated endpoint — PR metadata, not the merge sub-resource.
            "gh api repos/example-org/private-repo/pulls/12",
            # The literal subcommand form is still caught by the existing regex
            # and a separate test class; make sure unrelated reads don't trip the
            # api-endpoint branch either.
            "gh pr view 12",
        ],
    )
    def test_command_is_allowed_on_unmanaged_repo(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_existing_literal_subcommand_still_denied(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression guard: ``gh pr merge 12`` is still denied on managed repos."""
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 12", repo)) is True
        assert _parse_deny(capsys) is not None
