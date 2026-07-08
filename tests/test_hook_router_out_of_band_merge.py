# test-path: cross-cutting — drives hooks/scripts/hook_router.py + teatree.cli.teatree_gate; no src/teatree/ mirror.
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
import sqlite3
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import hooks.scripts.hook_router as router
from teatree.cli.teatree_gate import OUT_OF_BAND_MERGE_GATE_KEY, register_gate_commands
from teatree.config import cold_reader

_CONFIG_SETTING_SCHEMA = (
    'CREATE TABLE "teatree_config_setting" ('
    '"id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, '
    '"scope" varchar(255) NOT NULL, '
    '"key" varchar(255) NOT NULL, '
    '"value" text NOT NULL CHECK ((JSON_VALID("value") OR "value" IS NULL)), '
    '"created_at" datetime NOT NULL, '
    '"updated_at" datetime NOT NULL, '
    'CONSTRAINT "uniq_config_setting_scope_key" UNIQUE ("scope", "key"))'
)


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
            # pflag NO-SPACE shorthand — the bypass the cold-review flagged:
            # `-XPUT` is a real method override that the spaced-only regex
            # missed, so the merge slipped through. Must be DENIED.
            "gh api repos/example-org/private-repo/pulls/12/merge -XPUT",
            "gh api repos/example-org/private-repo/pulls/12/merge -XPOST",
            "gh api repos/example-org/private-repo/pulls/12/merge -XPATCH",
            # No-space last-wins: earlier GET overridden by trailing PUT → write.
            "gh api repos/example-org/private-repo/pulls/12/merge -XGET -XPUT",
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
            # No-space explicit GET — also a read (must not over-block).
            "gh api repos/example-org/private-repo/pulls/12/merge -XGET",
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


# ── Merge-TARGET classification — the cwd-only bypass (security) ──────────────
#
# The gate must classify the merge TARGET repo (extracted from the command),
# not the agent's cwd. Keying solely on the cwd meant a raw REST merge form —
# ``gh api --method PUT repos/souliane/teatree/pulls/N/merge`` or ``gh pr merge
# N --repo souliane/teatree`` — issued from ANY resolvable-but-UNMANAGED git
# cwd resolved cwd->unmanaged->ALLOW and merged a MANAGED repo's PR, bypassing
# the keystone MergeClear ceremony (reviewer!=loop, SHA-bind, live-CI recheck).
# Pre-fix, every command below returned False (allowed) from the unmanaged cwd.


class TestClassifiesMergeTargetNotCwd:
    """A managed-repo TARGET is blocked even from a resolvable, unmanaged cwd."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    @pytest.mark.parametrize(
        "command",
        [
            # GitHub REST PUT to teatree-core's merge endpoint (the cited bypass).
            "gh api --method PUT repos/souliane/teatree/pulls/123/merge",
            "gh api -X PUT repos/souliane/teatree/pulls/123/merge",
            # ``gh pr merge`` naming the managed target via ``--repo``.
            "gh pr merge 123 --repo souliane/teatree --squash",
            # An overlay-claimed managed repo named via ``--repo``.
            "gh pr merge 7 --repo example-org/private-repo",
            # GitLab REST POST to the managed overlay repo's merge endpoint
            # (url-encoded namespace decodes to example-org/private-repo).
            "glab api projects/example-org%2Fprivate-repo/merge_requests/9/merge --method POST",
            # Forge WEB-URL operand to ``gh pr merge`` — no --repo/api path, so the
            # slug must be parsed from the URL itself (GitHub /pull/<n>).
            "gh pr merge https://github.com/souliane/teatree/pull/123 --squash",
            # Forge WEB-URL operand to ``glab mr merge`` — GitLab /-/merge_requests/<n>
            # against the managed overlay namespace.
            "glab mr merge https://gitlab.com/example-org/private-repo/-/merge_requests/9",
            # GraphQL mergePullRequest mutation — target is an opaque node id the
            # slug parser cannot resolve, and the keystone never merges via raw
            # graphql, so this signature is blocked unconditionally (fail-closed).
            "gh api graphql -f query='mutation{mergePullRequest(input:{pullRequestId:\"PR_x\"}){clientMutationId}}'",
            # GraphQL enablePullRequestAutoMerge — merges on GitHub's native rules,
            # same merge effect as mergePullRequest under a different name.
            "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{pullRequestId:\"X\"})}'",
            # GraphQL mergeBranch — merges head ref into base ref out-of-band.
            'gh api graphql -f query=\'mutation{mergeBranch(input:{base:"a",head:"b"})}\'',
            # Uppercase URL path segment must still match (case-insensitive /PULL/).
            "gh pr merge https://github.com/souliane/teatree/PULL/9 --squash",
        ],
    )
    def test_managed_target_blocked_from_unmanaged_cwd(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # cwd is a resolvable, confidently-UNMANAGED repo — the bypass surface.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "ticket merge" in deny["permissionDecisionReason"]

    def test_sanctioned_keystone_still_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The keystone transition is not a raw gh/glab merge form, so the gate
        # never fires — even from inside the managed teatree-core checkout.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        assert router.handle_block_out_of_band_merge(_merge_event("t3 t3-teatree ticket merge 42", repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_unmanaged_target_from_unmanaged_cwd_still_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A genuinely-unmanaged target named via ``--repo`` from an unmanaged
        # cwd is still ALLOWED — the fix only blocks managed targets.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        command = "gh pr merge 3 --repo example-org/public-repo --squash"
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""

    def test_bare_number_from_unmanaged_cwd_still_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ``gh pr merge <n>`` with no --repo targets the cwd repo; from an
        # unmanaged cwd it must still ALLOW — the URL/graphql branches must not
        # fail-close the legitimate no-target case (#126 lockout guard).
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 5", repo)) is False
        assert capsys.readouterr().out.strip() == ""


# ── cwd classifier must not crash-fail-open with an overlay path configured ───
#
# `_cwd_is_teatree_managed` iterated each overlay `path` base with
# `cwd.resolve().relative_to(base)` under `suppress(OSError, RuntimeError)`.
# `Path.relative_to` raises `ValueError` for a non-subpath — NOT one of the
# suppressed types — so with ≥1 overlay path configured (the real config sets
# one) and a cwd NOT under it, the handler raised before the slug-based managed
# check ran. The crash-proof dispatcher catches the exception and fails OPEN,
# so a bare `gh pr merge <n>` / `glab mr merge !<n>` from a managed-by-slug
# checkout outside the overlay path bypassed the keystone. The fix swaps
# `relative_to` for the non-raising `is_relative_to`.


def _overlay_path_config(overlay_root: Path) -> str:
    return f'[overlays.example]\nworkspace_repos = ["example-org/private-repo"]\npath = "{overlay_root}"\n'


class TestCwdClassifyDoesNotCrashWithOverlayPath:
    """A configured overlay `path` must not crash the cwd-keyed classifier."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay_root = tmp_path / "overlay-root"
        overlay_root.mkdir()
        _patch_home(tmp_path / "home", _overlay_path_config(overlay_root), monkeypatch)

    @pytest.mark.parametrize("command", ["gh pr merge 5", "glab mr merge !9"])
    def test_bare_merge_from_managed_slug_cwd_outside_overlay_path_blocks(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # cwd is teatree-core (managed BY SLUG) but sits OUTSIDE the overlay
        # path, so the path loop must fall through to the slug check, not crash.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "ticket merge" in deny["permissionDecisionReason"]

    @pytest.mark.parametrize("command", ["gh pr merge 5", "glab mr merge !9"])
    def test_bare_merge_from_unmanaged_cwd_outside_overlay_path_allows(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Same overlay-path config, but an unmanaged-slug cwd outside the path:
        # the classifier must ALLOW (the fix classifies, it does not over-block).
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:example-org/public-repo.git")
        assert router.handle_block_out_of_band_merge(_merge_event(command, repo)) is False
        assert capsys.readouterr().out.strip() == ""


# ── raw-merge kill-switch (FIX-EXPEDITE PART B) ──────────────────────────────
#
# The gate was the only merge-adjacent gate denying via a bare emit; PART B gives
# it the ``out_of_band_merge_gate_enabled`` kill-switch (default on) routed through
# ``_fail_open_or_deny``, plus the ``t3 <overlay> gate raw-merge`` self-rescue CLI.


class TestRawMergeKillSwitch:
    """The ``out_of_band_merge_gate_enabled`` kill-switch: default-on denies, off allows."""

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_home(tmp_path / "home", _MANAGED_CONFIG, monkeypatch)

    def test_default_on_still_denies_managed_repo_merge(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Anti-vacuity: with no config the gate is enabled and the routing change
        # did NOT silently fail open — a managed-repo raw merge is still denied.
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 1", repo)) is True
        deny = _parse_deny(capsys)
        assert deny is not None
        assert "gate raw-merge disable" in deny["permissionDecisionReason"]

    def test_disabled_kill_switch_allows_managed_repo_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _repo_with_remote(tmp_path / "wt", "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(
            router,
            "_teatree_bool_setting",
            lambda key, default=True: False if key == "out_of_band_merge_gate_enabled" else default,
        )
        assert router.handle_block_out_of_band_merge(_merge_event("gh pr merge 1", repo)) is False
        assert capsys.readouterr().out.strip() == ""


class TestRawMergeGateCLI:
    """``t3 <overlay> gate raw-merge disable/enable/status`` self-rescue CLI."""

    def test_gate_key_constant(self) -> None:
        assert OUT_OF_BAND_MERGE_GATE_KEY == "out_of_band_merge_gate_enabled"

    def test_gate_raw_merge_subgroup_is_registered(self) -> None:
        overlay_app = typer.Typer()
        register_gate_commands(overlay_app)
        result = CliRunner().invoke(overlay_app, ["gate", "raw-merge", "status"])
        assert result.exit_code == 0
        assert "gate" in result.output.lower()

    def test_gate_raw_merge_disable_then_enable_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overlay_app = typer.Typer()
        register_gate_commands(overlay_app)
        runner = CliRunner()
        db = tmp_path / "db.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute(_CONFIG_SETTING_SCHEMA)
        conn.commit()
        conn.close()
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        disabled = runner.invoke(overlay_app, ["gate", "raw-merge", "disable"])
        assert disabled.exit_code == 0, disabled.output
        assert cold_reader.read_setting(OUT_OF_BAND_MERGE_GATE_KEY, scope="", db_path=db) is False
        enabled = runner.invoke(overlay_app, ["gate", "raw-merge", "enable"])
        assert enabled.exit_code == 0, enabled.output
        assert cold_reader.read_setting(OUT_OF_BAND_MERGE_GATE_KEY, scope="", db_path=db) is True
