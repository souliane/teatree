"""Eval matrix for the quote-scanner override and public-egress boundary (#1213, #126).

Legacy override tokens cannot bypass the public publish gate. This matrix
pins the production gate with those tokens present.

Scenario matrix:

* a clean body → ALLOW;
* ``QUOTE_OK=1`` in the process env cannot prevent a public block;
* an actual user-verbatim quote, no override → BLOCK;
* the body in a file the scanner cannot read → NOT auto-denied
    (treat as needs-inline, scan what it can — a missing draft file is
    not a leak).
"""

import json
from pathlib import Path

import pytest

from hooks.scripts.hook_router import handle_quote_scanner_pretool
from teatree.hooks import _repo_visibility
from teatree.hooks.quote_scanner import extract_publish_payload, scan_text


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestQuoteOkEnvReachesWrapper:
    """The override parses, while the public wrapper still enforces the gate."""

    def test_process_env_quote_ok_does_not_bypass_public_high_match_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("QUOTE_OK", "1")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = _bash('gh pr create -R souliane/teatree --title t --body "## User ask (verbatim, 2026-05-20)\nfoo"')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "QUOTE_OK" not in reason


class TestQuoteScannerGenuineGuardsIntact:
    """The override must not weaken the real block / clean-allow contract."""

    def test_public_post_ignores_env_override(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("QUOTE_OK", "1")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = _bash('gh pr create -R souliane/teatree --title t --body "## User ask (verbatim, 2026-05-20)\nship now"')
        assert handle_quote_scanner_pretool(data) is True
        reason = json.loads(capsys.readouterr().out)["permissionDecisionReason"]
        assert "QUOTE_OK" not in reason

    def test_clean_body_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash('gh pr create --title t --body "Refactored the config loader."')
        assert handle_quote_scanner_pretool(data) is False
        assert capsys.readouterr().err == ""

    def test_actual_user_quote_without_override_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The leak gate enforces ONLY on an affirmatively-public target (#1213), so
        # the genuine-violation guard posts to the public teatree repo with the
        # probe confirming it public.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = _bash(
            'gh pr create -R souliane/teatree --title t --body "## User ask (verbatim, 2026-05-20)\nplease ship now"'
        )
        assert handle_quote_scanner_pretool(data) is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"


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
        body_path.write_text("## User ask (verbatim, 2026-05-20)\nbody\n", encoding="utf-8")
        cmd = f"gh pr create --title t --body-file {body_path}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert scan_text(payload).has_high
