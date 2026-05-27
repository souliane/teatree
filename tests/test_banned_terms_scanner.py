"""Tests for the banned-terms posting gate (#1415).

The detection module ``teatree.hooks.banned_terms_scanner`` and its
PreToolUse handler ``handle_banned_terms_pretool`` together promote the
commit-only ``check-banned-terms.sh`` hook to the non-commit posting
surfaces (``gh issue/pr create|edit|comment``, ``glab mr|issue
note|create``, the ``gh api`` / ``glab api`` REST paths). It is the
sibling of the #1213 quote-scanner gate: it reuses the exact same
``_command_parser`` publish-surface detection + body extraction, then
delegates the *matching* to the existing ``check-banned-terms.sh``
against the ``~/.teatree.toml`` term list — it does NOT reimplement
matching.

These tests exercise the gate via real hook invocation: a clean body
passes, a banned-term body blocks, ``--body-file`` is read from disk.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_banned_terms_pretool
from teatree.hooks import banned_terms_scanner


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A ``~/.teatree.toml`` shaped config carrying one banned term."""
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text('[teatree]\nbanned_terms = ["acmecorp"]\n', encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def _pin_config(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the scanner at the test config instead of the real one."""
    monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(config))


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestScanText:
    def test_banned_term_is_matched(self, config: Path) -> None:
        term = banned_terms_scanner.scan_text("we ship to acmecorp next week", config_path=config)
        assert term == "acmecorp"

    def test_clean_text_returns_none(self, config: Path) -> None:
        assert banned_terms_scanner.scan_text("we ship next week", config_path=config) is None

    def test_match_is_case_insensitive(self, config: Path) -> None:
        assert banned_terms_scanner.scan_text("AcmeCorp ships", config_path=config) == "acmecorp"

    def test_email_only_match_is_ignored(self, config: Path) -> None:
        # Mirrors check-banned-terms.sh: a term only inside an email is allowed.
        text = "ping me at dev@acmecorp.example for details"
        assert banned_terms_scanner.scan_text(text, config_path=config) is None

    def test_empty_text_returns_none(self, config: Path) -> None:
        assert banned_terms_scanner.scan_text("", config_path=config) is None

    def test_missing_config_returns_none(self, tmp_path: Path) -> None:
        assert banned_terms_scanner.scan_text("acmecorp", config_path=tmp_path / "absent.toml") is None


class TestExtractPublishPayload:
    def test_gh_issue_create_body_is_extracted(self) -> None:
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": 'gh issue create --title t --body "ship to acmecorp"'}
        )
        assert payload is not None
        assert "acmecorp" in payload

    def test_non_publish_command_returns_none(self) -> None:
        assert banned_terms_scanner.extract_publish_payload("Bash", {"command": "ls -la"}) is None

    def test_non_bash_tool_returns_none(self) -> None:
        assert banned_terms_scanner.extract_publish_payload("Write", {"command": "x"}) is None

    def test_body_file_is_read_from_disk(self, tmp_path: Path) -> None:
        body_file = tmp_path / "body.md"
        body_file.write_text("internal note about acmecorp\n", encoding="utf-8")
        payload = banned_terms_scanner.extract_publish_payload(
            "Bash", {"command": f"gh pr create --title t --body-file {body_file}"}
        )
        assert payload is not None
        assert "acmecorp" in payload


class TestOverride:
    def test_flag_in_first_segment_bypasses(self) -> None:
        cmd = 'gh issue create --title t --body "acmecorp" --allow-banned-term'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is True

    def test_env_var_bypasses(self) -> None:
        tool_input = {"command": "gh issue create", "env": {"ALLOW_BANNED_TERM": "1"}}
        assert banned_terms_scanner.has_override("Bash", tool_input) is True

    def test_clean_command_has_no_override(self) -> None:
        assert banned_terms_scanner.has_override("Bash", {"command": "gh issue create --body x"}) is False

    def test_flag_after_metacharacter_does_not_bypass(self) -> None:
        # A flag smuggled into a second chained command must not bypass.
        cmd = 'gh issue create --body "acmecorp"; echo --allow-banned-term'
        assert banned_terms_scanner.has_override("Bash", {"command": cmd}) is False

    def test_non_bash_tool_has_no_flag_override(self) -> None:
        # A non-Bash tool can only override via the env mapping.
        assert banned_terms_scanner.has_override("Write", {"env": {"ALLOW_BANNED_TERM": "1"}}) is True
        assert banned_terms_scanner.has_override("Write", {}) is False


class TestScanTextFailOpen:
    def test_missing_script_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(banned_terms_scanner, "_scanner_script", lambda: Path("/nonexistent/check.sh"))
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None

    def test_subprocess_error_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _boom)
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None

    def test_scanner_crash_fails_open(self, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unexpected exit code (script itself failed) raises CommandFailedError
        # inside run_allowed_to_fail — the gate fails open rather than crash.
        def _crash(*_args: object, **_kwargs: object) -> None:
            raise banned_terms_scanner.CommandFailedError(["check"], 2, "", "boom")

        monkeypatch.setattr(banned_terms_scanner, "run_allowed_to_fail", _crash)
        assert banned_terms_scanner.scan_text("acmecorp", config_path=config) is None


class TestMatchedTerm:
    def test_term_in_flagged_line_is_reported(self) -> None:
        report = "BANNED TERM in /tmp/x.txt:\n  1:ship to acmecorp\n\nBanned terms: acmecorp, widgetco\n"
        assert banned_terms_scanner._matched_term(report) == "acmecorp"

    def test_falls_back_to_first_configured_term_when_no_flagged_line_matches(self) -> None:
        # The script reported a match but the term substring is not in any
        # flagged line we parsed — report the first configured term so the
        # deny reason is never empty.
        report = "BANNED TERM in /tmp/x.txt:\n\nBanned terms: acmecorp, widgetco\n"
        assert banned_terms_scanner._matched_term(report) == "acmecorp"

    def test_empty_report_returns_none(self) -> None:
        assert banned_terms_scanner._matched_term("") is None


@pytest.mark.integration
class TestHookHandlerEndToEnd:
    def test_clean_body_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "ship next week"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_banned_term_body_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "ship to acmecorp"'))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_body_file_is_read_and_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body_file = tmp_path / "issue_body.md"
        body_file.write_text("This affects acmecorp's deployment.\n", encoding="utf-8")
        blocked = handle_banned_terms_pretool(_bash(f"gh pr create --title t --body-file {body_file}"))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "acmecorp" in decision["permissionDecisionReason"]

    def test_gh_pr_comment_with_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh pr comment 5 --body "acmecorp asked for this"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_glab_mr_note_with_banned_term_blocks(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('glab mr note 5 --message "acmecorp wants this"'))
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_override_flag_bypasses_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash('gh issue create --title t --body "acmecorp" --allow-banned-term'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_non_publish_command_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash("ls -la"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_empty_body_publish_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_banned_terms_pretool(_bash("gh issue create --title t"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_missing_config_fails_open(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", "/nonexistent/.teatree.toml")
        blocked = handle_banned_terms_pretool(_bash('gh issue create --body "acmecorp"'))
        assert blocked is False
        assert capsys.readouterr().out == ""


class TestHookChainRegistration:
    def test_handler_is_wired_before_skill_load(self) -> None:
        chain = router._HANDLERS["PreToolUse"]
        names = [h.__name__ for h in chain]
        assert "handle_banned_terms_pretool" in names
        assert names.index("handle_banned_terms_pretool") < names.index("handle_enforce_skill_loading")


class TestFormatBlockMessage:
    def test_message_names_the_term_and_the_override(self) -> None:
        message = banned_terms_scanner.format_block_message("acmecorp")
        assert "acmecorp" in message
        assert "--allow-banned-term" in message
