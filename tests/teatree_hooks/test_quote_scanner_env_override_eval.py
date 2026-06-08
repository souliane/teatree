"""Eval matrix for the quote-scanner egress-wrapper escape hatch (#1213, #126).

The gate-over-deny lockout this guards against: the documented
``QUOTE_OK=1`` escape did NOT propagate to the egress wrapper. The
override check read ONLY ``tool_input["env"]`` — but the Claude Code
PreToolUse payload for a ``Bash`` tool carries no ``env`` block, so the
agent's ``QUOTE_OK=1`` process env var (which the hook subprocess
inherits via ``os.environ``) never reached the gate. The escape was
documented in every block message yet structurally unreachable, forcing
paraphrase workarounds all session.

Scenario matrix (accepts a legitimately-authorized action / still
blocks a genuine violation / the documented escape actually works /
fails-OPEN on a broken env):

* a clean body → ALLOW;
* ``QUOTE_OK=1`` in the process env (``os.environ``) → ALLOW;
* an actual user-verbatim quote, no override → BLOCK;
* the body in a file the scanner cannot read → NOT auto-denied
    (treat as needs-inline, scan what it can — a missing draft file is
    not a leak).
"""

import json
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_quote_scanner_pretool
from teatree.hooks.quote_scanner import extract_publish_payload, has_quote_ok_override, scan_text


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestQuoteOkEnvReachesWrapper:
    """``QUOTE_OK=1`` in the process env bypasses the gate (the documented escape)."""

    def test_process_env_quote_ok_is_honoured_by_override_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUOTE_OK", "1")
        cmd = 'gh pr create --title t --body "the user said: ship it now"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_process_env_quote_ok_zero_does_not_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUOTE_OK", "0")
        cmd = 'gh pr create --title t --body "the user said: ship it now"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_process_env_quote_ok_bypasses_high_match_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("QUOTE_OK", "1")
        data = _bash('gh pr create --title t --body "## User mandate\nfoo"')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_tool_input_env_still_honoured(self) -> None:
        # The legacy ``tool_input["env"]`` surface keeps working — a
        # harness that DOES populate it is not regressed.
        cmd = 'gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd, "env": {"QUOTE_OK": "1"}}) is True


class TestQuoteScannerGenuineGuardsIntact:
    """The override must not weaken the real block / clean-allow contract."""

    def test_clean_body_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash('gh pr create --title t --body "Refactored the config loader."')
        assert handle_quote_scanner_pretool(data) is False
        assert capsys.readouterr().err == ""

    def test_actual_user_quote_without_override_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash('gh pr create --title t --body "## User mandate\nplease ship now"')
        assert handle_quote_scanner_pretool(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    def test_no_env_var_means_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QUOTE_OK", raising=False)
        cmd = 'gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False


class TestUnreadableBodyFileIsNotAutoDenied:
    """A ``--body-file`` the scanner cannot read is needs-inline, not a leak (#126)."""

    def test_missing_gh_body_file_does_not_fail_closed(self) -> None:
        # A drafted ``--body-file`` that does not exist at scan time (the
        # agent writes it later, or it was a typo) carries no body — the
        # gate must NOT manufacture a fail-closed HIGH finding out of an
        # absent file. Nothing to scan ⇒ no leak.
        cmd = "gh pr create --title t --body-file /nonexistent/draft-126.md"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert not scan.has_high, f"a missing --body-file must not auto-deny; findings={scan.findings!r}"

    def test_missing_gh_body_file_end_to_end_does_not_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash("gh pr create --title t --body-file /nonexistent/draft-126.md")
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_readable_body_file_with_quote_still_blocks(self, tmp_path: Path) -> None:
        # The carve-out is ONLY for an unreadable file — a readable
        # body-file carrying a verbatim quote still trips the gate.
        body_path = tmp_path / "pr.md"
        body_path.write_text("## User directive\nbody\n", encoding="utf-8")
        cmd = f"gh pr create --title t --body-file {body_path}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert scan_text(payload).has_high
