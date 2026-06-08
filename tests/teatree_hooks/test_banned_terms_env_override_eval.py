"""Eval matrix for the banned-terms egress-wrapper escape hatch (#1415, #126).

The gate-over-deny lockout this guards against: the documented
``--allow-banned-term`` / ``ALLOW_BANNED_TERM=1`` override did NOT
propagate through the wrapper. The override check read ONLY
``tool_input["env"]``, but the Claude Code PreToolUse payload for a
``Bash`` tool carries no ``env`` block — the agent's
``ALLOW_BANNED_TERM=1`` process env var (inherited by the hook
subprocess via ``os.environ``) never reached the gate, forcing
numeric-id + paraphrase workarounds all session.

Scenario matrix:

* the ``ALLOW_BANNED_TERM=1`` override in the process env → ALLOW;
* no override + a banned term in a POST → BLOCK;
* fails OPEN on a broken env (no config / unreadable env).
"""

import json
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks.banned_terms_scanner import has_override, scan_text


@pytest.fixture
def _term_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin a one-term banned-list config so the shell scanner has something to flag."""
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
    monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(cfg))
    return cfg


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestAllowBannedTermEnvReachesWrapper:
    """``ALLOW_BANNED_TERM=1`` in the process env bypasses the gate."""

    def test_process_env_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_BANNED_TERM", "1")
        cmd = 'gh issue create --title t --body "mention of acmecorp here"'
        assert has_override("Bash", {"command": cmd}) is True

    def test_process_env_override_zero_does_not_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALLOW_BANNED_TERM", "0")
        cmd = 'gh issue create --title t --body "mention of acmecorp here"'
        assert has_override("Bash", {"command": cmd}) is False

    def test_tool_input_env_still_honoured(self) -> None:
        cmd = 'gh issue create --title t --body "acmecorp"'
        assert has_override("Bash", {"command": cmd, "env": {"ALLOW_BANNED_TERM": "1"}}) is True

    def test_flag_in_first_segment_still_honoured(self) -> None:
        cmd = 'gh issue create --title t --body "acmecorp" --allow-banned-term'
        assert has_override("Bash", {"command": cmd}) is True

    def test_inline_env_behind_cd_prefix_is_honoured(self) -> None:
        # The common sub-agent shape: cd into the worktree, then commit with the
        # override leading the publish segment.
        cmd = 'cd /work/ticket && ALLOW_BANNED_TERM=1 git commit -m "acmecorp"'
        assert has_override("Bash", {"command": cmd}) is True

    @pytest.mark.usefixtures("_term_config")
    def test_process_env_override_bypasses_block_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ALLOW_BANNED_TERM", "1")
        data = _bash('gh issue create --title t --body "acmecorp ships next week"')
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    @pytest.mark.usefixtures("_term_config")
    def test_inline_env_behind_cd_prefix_bypasses_block_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
        data = _bash('cd /tmp && ALLOW_BANNED_TERM=1 gh issue create --title t --body "acmecorp ships"')
        blocked = handle_banned_terms_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""


class TestBannedTermGenuineGuardIntact:
    """The override must not weaken the real block on a genuine violation."""

    @pytest.mark.usefixtures("_term_config")
    def test_banned_term_in_post_without_override_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
        data = _bash('gh issue create --title t --body "acmecorp ships next week"')
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "acmecorp" in out["permissionDecisionReason"]

    def test_clean_body_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
        cmd = 'gh issue create --title t --body "acmecorp"'
        # No override env, no flag → no bypass (the block decision is then
        # made by the scanner against the config).
        assert has_override("Bash", {"command": cmd}) is False

    @pytest.mark.usefixtures("_term_config")
    def test_override_on_decoy_segment_does_not_bypass_chained_publish(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The override leads a harmless echo; bash scopes it to that command, so
        # it must NOT vouch for the banned-term publish chained after it.
        monkeypatch.delenv("ALLOW_BANNED_TERM", raising=False)
        data = _bash('ALLOW_BANNED_TERM=1 echo hi && gh issue create --title t --body "acmecorp ships"')
        blocked = handle_banned_terms_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


class TestBannedTermFailsOpenOnBrokenEnv:
    """A missing config / unreadable scanner must fail OPEN (no block)."""

    def test_no_config_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(tmp_path / "does-not-exist.toml"))
        # No config ⇒ scan_text returns None ⇒ the gate never blocks.
        assert scan_text("acmecorp leak") is None
