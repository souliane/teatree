"""Adversarial leak-gate liveness after the U17 router package-split.

The U17 refactor package-ified ``hooks/scripts`` and moved handlers into a
per-domain ``handlers/`` package behind the ``_HANDLERS`` routing table. That is
pure reorganisation — every leak-prevention gate must fire IDENTICALLY before and
after. This test is the standing proof that a known banned term AND a known secret
pattern are STILL BLOCKED post-split, driven two ways.

The SECRET path is proved end-to-end through the REAL router subprocess (the live
hook's ``python3 hook_router.py --event PreToolUse`` invocation): a secret is
blocked on every surface regardless of destination, so this is deterministic
without a visibility probe and proves the split preserves the whole subprocess
deny path. The BANNED-TERM path is proved in-process through the re-exported
``handle_banned_terms_pretool`` with an affirmatively-PUBLIC destination (the
visibility probe is pinned, exactly as ``test_public_leak_gate_stays_fail_closed``
does), because the banned-term deny scopes to an affirmatively-public surface a
subprocess cannot resolve without an authenticated ``gh`` on PATH.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import _repo_visibility

_HOOK_ROUTER = Path(__file__).resolve().parents[2] / "hooks" / "scripts" / "hook_router.py"

# The classic AWS example access key — a well-known FAKE credential that matches
# the ``AKIA[A-Z0-9]{16}`` secret pattern without being a real secret.
_FAKE_SECRET = "AKIAIOSFODNN7EXAMPLE"
_BANNED_TERM = "acmecorp"


def _seed_config_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
            (json.dumps([_BANNED_TERM]),),
        )
        conn.commit()
    finally:
        conn.close()


def _public_post_with(payload: str) -> str:
    return f'gh issue create --repo souliane/teatree --title t --body "{payload}"'


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestSecretBlockedEndToEndSubprocess:
    """The secret deny fires end-to-end through the real router subprocess (exit 2)."""

    @pytest.fixture
    def hook_env(self, tmp_path: Path) -> dict[str, str]:
        _seed_config_db(tmp_path / "config.sqlite3")
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "T3_CONFIG_DB": str(tmp_path / "config.sqlite3"),
            "T3_DATA_DIR": str(tmp_path / "data"),
            "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(tmp_path / "state"),
        }

    def _run(self, command: str, env: dict[str, str]) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(_HOOK_ROUTER), "--event", "PreToolUse"],
            input=json.dumps(_bash(command)),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, proc.stdout

    def test_secret_pattern_is_still_blocked(self, hook_env: dict[str, str]) -> None:
        rc, out = self._run(_public_post_with(f"deploy key {_FAKE_SECRET} now"), hook_env)
        assert rc == 2, "a secret pattern must still deny (exit 2) after the router split"
        decision = json.loads(out)
        assert decision["permissionDecision"] == "deny"
        assert "secret" in decision["permissionDecisionReason"].lower()

    def test_benign_command_still_passes(self, hook_env: dict[str, str]) -> None:
        rc, out = self._run(_public_post_with("just a normal update"), hook_env)
        assert rc == 0, "a benign public post must pass (exit 0) — the gate must not over-block"
        assert out == ""


class TestBannedTermBlockedInProcess:
    """The banned-term deny fires through the re-exported handler on a public surface."""

    @pytest.fixture(autouse=True)
    def _public_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_config_db(tmp_path / "config.sqlite3")
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
        monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")

    def test_banned_term_is_still_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash(_public_post_with(f"rolling out {_BANNED_TERM} integration")))
        assert blocked is True, "banned term on a public surface must still deny after the router split"
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "banned-terms" in decision["permissionDecisionReason"]

    def test_benign_body_is_not_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash(_public_post_with("just a normal update")))
        assert not blocked, "a benign body must not be blocked — the gate must not over-block"
        assert capsys.readouterr().out == ""

    def test_banned_handler_is_the_routing_table_entry(self) -> None:
        # The re-exported handler the split moved through is the SAME object the
        # router dispatches — a decomposition that silently dropped the gate from
        # _HANDLERS would leave the term unblocked in production.
        assert handle_banned_terms_pretool in router._HANDLERS["PreToolUse"]
