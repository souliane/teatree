"""Tests for the pre-publish quote-scanner gate (#1213).

The detection module ``teatree.hooks.quote_scanner`` and its
PreToolUse handler ``handle_quote_scanner_pretool`` together promote
the prose-only "never quote user verbatim" rule to a deterministic
tooling gate. These tests exercise both halves end-to-end: each
representative publish surface (Bash gh/glab/git/curl, the t3 review
commands, the Slack MCP send) is given a realistic body and the gate's
decision plus side effects (deny JSON, stderr warning, ledger entry)
are asserted as a unit.
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_quote_scanner_pretool
from teatree.hooks import quote_scanner
from teatree.hooks.quote_scanner import Finding, ScanResult, extract_publish_payload, has_quote_ok_override, scan_text


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the ledger + blocklist root to ``tmp_path`` so tests don't touch real state."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _ledger_lines(tmp_path: Path) -> list[dict[str, object]]:
    ledger = tmp_path / "quote-scanner.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


class TestScanTextHighPatterns:
    """Each HIGH pattern in the built-in catalogue produces a HIGH finding."""

    @pytest.mark.parametrize(
        ("body", "expected_name"),
        [
            ("## User mandate\n\nplease ship it", "heading-user-mandate"),
            ("### User feedback (paraphrased): xyz", "heading-user-mandate"),
            ("## User ask (verbatim, 2026-05-20)\nbody", "heading-user-ask-verbatim"),
            ("**User directive (verbatim, today):** body", "bold-user-directive-verbatim"),
            ('> "An imperative sentence the user spoke."', "blockquote-attributed"),
            ('A direct phrase like _"this is a long enough sentence to trip the gate"_.', "italic-quote-long"),
            ('Per user feedback "ship it now"', "per-user-feedback-quoted"),
            ("Per the user said: ship it now", "the-user-said-colon"),
        ],
    )
    def test_each_high_pattern_is_flagged(self, body: str, expected_name: str) -> None:
        result = scan_text(body)
        assert result.has_high, f"expected HIGH finding for {expected_name!r}, got {result.findings!r}"
        assert any(f.name == expected_name for f in result.high)


class TestScanTextMediumPatterns:
    @pytest.mark.parametrize(
        ("body", "expected_name"),
        [
            ("Per user direction we ship this Friday.", "per-user-direction"),
            ("RED CARD from user on the trailer policy.", "red-card-from-user"),
            ("the user has explicitly approved the rollback.", "the-user-has-verb"),
        ],
    )
    def test_each_medium_pattern_warns(self, body: str, expected_name: str) -> None:
        result = scan_text(body)
        assert result.has_medium
        assert not result.has_high
        assert any(f.name == expected_name for f in result.medium)


class TestBlocklistFile:
    def test_regex_blocklist_compiles_and_matches_case_insensitive(self, tmp_path: Path) -> None:
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text(
            "# comment\n\n^Operation\\s+Greenlight\\b\n",
            encoding="utf-8",
        )
        result = scan_text("Operation Greenlight begins tomorrow.", blocklist_path=blocklist)
        assert result.has_high
        assert any(f.name.startswith("blocklist:") for f in result.high)

    def test_invalid_regex_raises_clear_error(self, tmp_path: Path) -> None:
        blocklist = tmp_path / "blocklist.txt"
        blocklist.write_text("[unterminated\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid regex"):
            scan_text("anything", blocklist_path=blocklist)


class TestExtractPublishPayloadBash:
    def test_gh_issue_create_with_double_quoted_body(self) -> None:
        cmd = 'gh issue create --title t --body "the user said: ship it now"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "the user said" in payload

    def test_git_commit_minus_m_single_quoted_body(self) -> None:
        cmd = "git commit -m 'fix: x\n\n## User mandate\nbody'"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_heredoc_body_via_cat_eof_is_captured(self) -> None:
        cmd = (
            "gh pr create --title t --body \"$(cat <<'EOF'\n"
            "Summary line.\n\n"
            '> "A direct attributed quote from the user."\n'
            "EOF\n"
            ')"'
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "attributed quote" in payload

    def test_body_file_arg_is_read_from_disk(self, tmp_path: Path) -> None:
        body_path = tmp_path / "pr.md"
        body_path.write_text("## User directive\nbody\n", encoding="utf-8")
        cmd = f"gh pr create --title t --body-file {body_path}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User directive" in payload

    def test_non_publish_command_returns_none(self) -> None:
        assert extract_publish_payload("Bash", {"command": "ls -la"}) is None

    def test_curl_chat_post_message_is_a_publish_surface(self) -> None:
        cmd = 'curl -X POST https://slack.com/api/chat.postMessage -d \'{"text":"the user said: ship it now"}\''
        payload = extract_publish_payload("Bash", {"command": cmd})
        # The curl ``-d`` flag is parsed by :func:`_extract_curl_payloads`
        # so the JSON ``text`` field is included in the scan payload.
        assert payload is not None
        assert "the user said" in payload


class TestT3PublishCommands:
    @pytest.mark.parametrize(
        "subcommand",
        [
            "t3 teatree notify send",
            "t3 teatree review post-comment",
            "t3 teatree review post-draft-note",
            "t3 mycustomer review post-comment",
            "t3 teatree ticket create-issue",
            "t3 slack react",
        ],
    )
    def test_publish_surface_recognised(self, subcommand: str) -> None:
        cmd = f'{subcommand} --body "## User mandate\nplease ship"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload


class TestQuoteOkOverride:
    def test_flag_in_bash_command_bypasses_check(self) -> None:
        cmd = 'gh pr create --title t --body "the user said: foo" --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_env_var_in_tool_input_bypasses_check(self) -> None:
        cmd = 'gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd, "env": {"QUOTE_OK": "1"}}) is True

    def test_clean_command_has_no_override(self) -> None:
        assert has_quote_ok_override("Bash", {"command": "gh pr create --title t --body x"}) is False

    def test_quote_ok_substring_inside_quoted_body_does_not_count(self) -> None:
        # The flag-detection uses shlex tokens — a literal "--quote-ok"
        # substring inside a double-quoted body arg is NOT a token of
        # its own, so the override does not fire.
        cmd = 'gh pr create --title t --body "discussion of --quote-ok semantics"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_quote_ok_smuggled_after_shell_comment_is_rejected(self) -> None:
        # Codex CRITICAL #1: ``# --quote-ok`` after a publish command must
        # NOT bypass the gate. ``shlex.split`` must strip comments.
        cmd = 'gh issue comment 1 --body "leak" # --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_quote_ok_smuggled_after_metacharacter_is_rejected(self) -> None:
        # Override must not fire when it lives after a shell metacharacter
        # — even if it parses as a token, it is not part of the publish
        # invocation we are gating.
        for metachar in (";", "|", "&&"):
            cmd = f'gh issue comment 1 --body "leak" {metachar} echo --quote-ok'
            assert has_quote_ok_override("Bash", {"command": cmd}) is False, (
                f"override smuggled after {metachar!r} must be rejected"
            )


class TestBypassClosures:
    """Regression tests for codex-found bypass paths (#1213 review)."""

    # --- CRITICAL #2: glab note without 'create' segment ---

    def test_glab_mr_note_no_create_is_a_publish_surface(self) -> None:
        cmd = 'glab mr note 42 -m "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_glab_issue_note_no_create_is_a_publish_surface(self) -> None:
        cmd = 'glab issue note 17 -m "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- CRITICAL #3: gh short -b body flag ---

    def test_gh_pr_comment_short_b_body_is_parsed(self) -> None:
        cmd = 'gh pr comment 5 -b "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_gh_issue_comment_short_b_body_is_parsed(self) -> None:
        cmd = 'gh issue comment 5 -b "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- CRITICAL #4: gh api / glab api comment POSTs ---

    def test_gh_api_is_a_publish_surface_with_field_body(self) -> None:
        cmd = 'gh api repos/x/y/issues/1/comments -f body="## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_gh_api_uppercase_f_field_body_is_parsed(self) -> None:
        cmd = 'gh api repos/x/y/issues/1/comments -F body="## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_gh_api_raw_field_body_is_parsed(self) -> None:
        cmd = 'gh api repos/x/y/issues/1/comments --raw-field body="## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_glab_api_is_a_publish_surface_with_field_body(self) -> None:
        cmd = 'glab api projects/1/issues/1/notes -f body="## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_gh_api_input_file_payload_is_read(self, tmp_path: Path) -> None:
        body_path = tmp_path / "comment.json"
        body_path.write_text('{"body": "## User mandate\\nship it"}', encoding="utf-8")
        cmd = f"gh api repos/x/y/issues/1/comments --input {body_path}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- CRITICAL #5: curl data-flag JSON parsing ---

    def test_curl_data_flag_json_text_field_is_parsed(self) -> None:
        cmd = 'curl -X POST https://slack.com/api/chat.postMessage -d \'{"text":"## User mandate\\nplease ship"}\''
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_curl_data_raw_flag_json_message_field_is_parsed(self) -> None:
        cmd = (
            "curl -X POST https://slack.com/api/chat.postMessage "
            '--data-raw \'{"message":"## User mandate\\nplease ship"}\''
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_curl_json_flag_body_field_is_parsed(self) -> None:
        cmd = 'curl -X POST https://example.com/api/comments --json \'{"body":"## User mandate\\nplease ship"}\''
        # The --json curl flag is publish-shaped here only if the URL
        # matches an external publish surface — but for this test the
        # parser shouldn't care about the URL, it just needs to extract
        # the body. We assert via _extract_bash_payload directly.
        from teatree.hooks.quote_scanner import _extract_bash_payload  # noqa: PLC0415

        body = _extract_bash_payload(cmd)
        assert "User mandate" in body

    def test_curl_data_flag_unparseable_json_fails_closed(self) -> None:
        # Fail-closed: when curl carries a data flag we cannot parse,
        # the payload must contain a sentinel string that will trip the
        # HIGH gate (we use the well-known HIGH pattern so the test does
        # not depend on a new pattern). Specifically: a `the user said:`
        # marker so any reviewer sees the gate blocked.
        cmd = "curl -X POST https://slack.com/api/chat.postMessage -d @some-binary-file"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        # The fail-closed sentinel deliberately matches a HIGH pattern so
        # downstream ``scan_text`` produces a deny decision.
        scan = scan_text(payload)
        assert scan.has_high, (
            f"unparsable curl data must fail closed via a HIGH-matching sentinel; got payload={payload!r}"
        )


class TestHookHandlerEndToEnd:
    def test_high_match_emits_deny_and_breaks_chain(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash('gh pr create --title t --body "## User mandate\nplease ship now"')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"
        assert "quote-scanner" in decision["permissionDecisionReason"]
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "deny"

    def test_medium_only_warns_on_stderr_and_allows_publish(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        data = _bash('gh pr create --title t --body "Per user direction, we ship Friday."')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "attribution" in captured.err.lower()
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "warn"

    def test_quote_ok_override_bypasses_high_match(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = _bash('gh pr create --title t --body "## User mandate\nfoo" --quote-ok')
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False
        assert capsys.readouterr().out == ""
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "allow-override"
        assert ledger[-1]["override"] is True

    def test_non_publish_bash_command_is_a_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_quote_scanner_pretool(_bash("ls -la"))
        assert blocked is False
        assert capsys.readouterr().out == ""
        # A noop never reaches the scan path, so the ledger stays empty.
        assert _ledger_lines(tmp_path) == []

    def test_empty_body_is_allowed_silently(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # A publish surface with no captured body (e.g. ``gh pr create``
        # without any --body* arg) hits the scanner with an empty
        # payload — clean by construction, no findings, no warning.
        blocked = handle_quote_scanner_pretool(_bash("gh pr create --title t"))
        assert blocked is False
        captured = capsys.readouterr()
        assert captured.err == ""
        ledger = _ledger_lines(tmp_path)
        assert ledger
        assert ledger[-1]["decision"] == "allow"

    def test_clean_body_with_unattributed_quote_is_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A body that happens to contain the word "user" or a generic
        # quoted string but no user-attributed shape must pass.
        body = "Summary: refactored the user-config loader to drop dead branches."
        blocked = handle_quote_scanner_pretool(_bash(f'gh pr create --title t --body "{body}"'))
        assert blocked is False
        assert capsys.readouterr().err == ""

    def test_slack_mcp_send_message_is_scanned(self, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "tool_name": "mcp__claude_ai_Slack__slack_send_message",
            "tool_input": {"text": "## User mandate\nplease ship"},
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    def test_multiline_heredoc_with_high_pattern_is_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = (
            "gh pr create --title t --body \"$(cat <<'EOF'\n"
            "Summary of the change.\n\n"
            "## User ask (verbatim, 2026-05-20)\n"
            "do the thing\n"
            "EOF\n"
            ')"'
        )
        blocked = handle_quote_scanner_pretool(_bash(cmd))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"
        assert "heading-user-ask-verbatim" in out["permissionDecisionReason"]


class TestHookChainRegistration:
    def test_handler_is_wired_before_skill_load(self) -> None:
        chain = router._HANDLERS["PreToolUse"]
        names = [h.__name__ for h in chain]
        assert "handle_quote_scanner_pretool" in names
        assert names.index("handle_quote_scanner_pretool") < names.index("handle_enforce_skill_loading")


class TestFormatHelpers:
    def test_block_message_lists_matched_pattern_names(self) -> None:
        result = ScanResult(findings=[Finding(name="heading-user-mandate", severity=quote_scanner.HIGH, excerpt="x")])
        message = quote_scanner.format_block_message(result)
        assert "heading-user-mandate" in message
        assert "--quote-ok" in message

    def test_warn_message_lists_matched_pattern_names(self) -> None:
        result = ScanResult(findings=[Finding(name="per-user-direction", severity=quote_scanner.MEDIUM, excerpt="x")])
        message = quote_scanner.format_warn_message(result)
        assert "per-user-direction" in message
