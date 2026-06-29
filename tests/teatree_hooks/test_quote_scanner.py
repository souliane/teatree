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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_quote_scanner_pretool
from teatree.hooks import _repo_visibility, quote_scanner
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL, is_fail_closed_sentinel
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

    def test_flag_on_cd_prefixed_publish_segment_bypasses(self) -> None:
        cmd = 'cd /tmp/wt && gh pr create --title t --body "the user said: foo" --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_flag_on_second_chained_publish_segment_bypasses(self) -> None:
        cmd = 'echo prep && gh pr create --title t --body "the user said: foo" --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_decoy_flag_on_unrelated_segment_does_not_vouch_for_chained_publish(self) -> None:
        cmd = 'echo --quote-ok && gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_decoy_flag_on_trailing_non_publish_segment_does_not_vouch(self) -> None:
        cmd = 'gh pr create --title t --body "the user said: foo" ; echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_inline_env_assignment_on_cd_prefixed_publish_bypasses(self) -> None:
        cmd = 'cd /tmp/wt && QUOTE_OK=1 gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_inline_env_assignment_on_leading_publish_bypasses(self) -> None:
        cmd = 'QUOTE_OK=1 gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is True

    def test_decoy_inline_env_assignment_on_unrelated_segment_does_not_vouch(self) -> None:
        cmd = 'QUOTE_OK=1 echo hi && gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_inline_env_assignment_zero_does_not_bypass(self) -> None:
        cmd = 'QUOTE_OK=0 gh pr create --title t --body "the user said: foo"'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False


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

    def test_gh_api_field_body_cat_substitution_file_is_scanned(self, tmp_path: Path) -> None:
        body_path = tmp_path / "note.md"
        body_path.write_text("## User mandate\nship it", encoding="utf-8")
        cmd = f'gh api repos/x/y/issues/1/comments -f body="$(cat {body_path})"'
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
        # Fail-closed: when curl carries a data flag we cannot parse, the
        # payload carries the fail-closed sentinel, which ``scan_text``
        # recognises EXPLICITLY as a HIGH finding (#126) rather than via a
        # content-pattern self-match.
        cmd = "curl -X POST https://slack.com/api/chat.postMessage -d @some-binary-file"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high, (
            f"unparsable curl data must fail closed via the explicit sentinel finding; got payload={payload!r}"
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


class TestHookHandlerFailOpenWithoutTeatreeImport:
    """Regression for #1314.

    The hook script is invoked from the user's session shell with no
    guarantee that ``teatree`` is already importable on ``sys.path``.
    A failure to import (or any other internal scanner error) must
    fail open: the handler returns ``False`` (no block, no traceback)
    so the tool use proceeds unchanged. A crashing PreToolUse hook
    leaks the traceback to stderr on every Bash invocation and is
    strictly worse than no scan.
    """

    def test_handler_returns_false_when_teatree_unimportable(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The test module's own ``from teatree.hooks import quote_scanner``
        # at line 23 has already cached ``teatree``, ``teatree.hooks``, and
        # ``teatree.hooks.quote_scanner`` in ``sys.modules``. Patching only
        # ``sys.modules["teatree"] = None`` is a no-op because the
        # production handler's ``from teatree.hooks import quote_scanner``
        # resolves the submodule from cache without re-attempting the
        # parent lookup. Wipe all three cache entries before re-patching
        # the parent to ``None`` so the next ``from teatree.hooks import
        # quote_scanner`` is forced to re-resolve and raises ImportError.
        for mod in ("teatree.hooks.quote_scanner", "teatree.hooks", "teatree"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.setitem(sys.modules, "teatree", None)
        blocked = handle_quote_scanner_pretool(_bash("echo test"))
        assert blocked is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_handler_returns_false_on_arbitrary_internal_exception(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An internal scanner error (regex compile, ledger I/O,
        # blocklist parse) must not crash the hook chain either.
        def _boom(*_args: object, **_kwargs: object) -> None:
            msg = "synthetic"
            raise RuntimeError(msg)

        monkeypatch.setattr(quote_scanner, "extract_publish_payload", _boom)
        blocked = handle_quote_scanner_pretool(_bash('gh pr create --title t --body "foo"'))
        assert blocked is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_subprocess_invocation_without_teatree_on_path_does_not_traceback(self, tmp_path: Path) -> None:
        # End-to-end reproducer for #1314: invoke the hook script as a
        # fresh subprocess with an interpreter that does NOT have
        # ``teatree`` installed. ``sys.executable`` cannot be used here
        # because it points at the editable-install ``.venv`` python
        # whose ``site-packages/teatree.pth`` puts ``teatree`` on
        # ``sys.path`` at startup regardless of ``PYTHONPATH`` —
        # stripping ``PYTHONPATH`` would not actually unimport teatree.
        # ``uv run --isolated --no-project python`` gives us a clean
        # interpreter with no editable install and no ``.pth`` for the
        # project; the hook must bootstrap ``sys.path`` from
        # ``parents[2] / src`` and exit cleanly without leaking a
        # traceback.
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not on PATH; no teatree-free interpreter available")
        hook_script = Path(router.__file__).resolve()
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "echo test"},
                "session_id": "diag",
            }
        )
        # Confirm the chosen interpreter genuinely lacks teatree before
        # using it as the reproducer — otherwise the test would silently
        # pass against the broken pre-fix code.
        precheck = subprocess.run(
            [uv, "run", "--isolated", "--no-project", "python", "-c", "import teatree"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if precheck.returncode == 0:
            pytest.skip("uv --isolated python still imports teatree; no reproducible env")
        result = subprocess.run(
            [
                uv,
                "run",
                "--isolated",
                "--no-project",
                "python",
                str(hook_script),
                "--event",
                "PreToolUse",
            ],
            input=payload,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, f"exit={result.returncode}, stderr={result.stderr!r}"
        assert "Traceback" not in result.stderr
        assert "ModuleNotFoundError" not in result.stderr


class TestRound2BypassClosures:
    """Regression tests for the 7 round-2 codex-found bypass paths.

    Each test reproduces a distinct bypass surfaced in the codex
    re-verdict comment on PR #1251 (re-review of commit ``e8b642cc``).
    Test names align 1:1 with the round-2 finding numbers.
    """

    # --- Round-2 #1: newline-separated ``--quote-ok`` override ---

    def test_override_smuggled_after_literal_newline_is_rejected(self) -> None:
        # ``gh ... --body "leak"\n--quote-ok`` — the override token must
        # only count as a CLI token in the FIRST shell command, not as
        # text after a literal newline (which acts as a shell separator
        # at the command level).
        cmd = 'gh issue comment 1 --body "## User mandate\nbody"\n--quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_override_smuggled_after_carriage_return_is_rejected(self) -> None:
        cmd = 'gh issue comment 1 --body "## User mandate\nbody"\r\n--quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_high_match_with_newline_smuggled_override_still_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # End-to-end: the gate must DENY when the only override token
        # was smuggled past a literal newline.
        cmd = 'gh issue comment 1 --body "## User mandate\nbody"\n--quote-ok'
        blocked = handle_quote_scanner_pretool(_bash(cmd))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"

    # --- Round-2 #2: line-continuation `\` in publish command ---

    def test_line_continuation_in_publish_command_still_parses_body(self) -> None:
        # ``gh issue \\<NL> comment 1 --body "..."`` — bash joins the
        # continued line into a single command, so the body must still
        # be extracted.
        cmd = 'gh issue \\\n  comment 1 --body "## User mandate\nbody"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_line_continuation_does_not_smuggle_override(self) -> None:
        # Splitting ``--quote-ok`` across a backslash-newline must not
        # bypass the override check — the joined token is ``--quote-ok``
        # which IS a legitimate override (this case should fire), but
        # the FIRST-segment rule still applies. Place the override AFTER
        # a metacharacter to confirm it is rejected.
        cmd = 'gh issue comment 1 --body "leak" \\\n  ; echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    # --- Round-2 #3: ANSI-C $'...' quoting ---

    def test_ansi_c_body_quoting_is_decoded_and_scanned(self) -> None:
        # Bash decodes ``$'\n'`` to a literal newline before passing the
        # value as a single arg. The scanner must see the decoded body
        # so a ``## User mandate`` heading inside ``$'...'`` is caught.
        cmd = r"""gh issue create --title t --body $'## User mandate\nship it'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_ansi_c_hex_escapes_are_decoded_and_scanned(self) -> None:
        # ``$'\x4c\x65\x61\x6b'`` decodes to ``Leak`` — make sure the
        # scanner sees the decoded literal so an obfuscation via
        # hex-escape cannot smuggle a HIGH match past detection.
        cmd = r"""gh issue create --title t --body $'## User mandate\n\x73\x68\x69\x70 it'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload
        assert "ship" in payload

    def test_ansi_c_undecodable_fails_closed(self) -> None:
        # If a body uses ANSI-C quoting and the value contains a body-
        # flag flag but the content is opaque, we still pull whatever
        # shlex extracted so subsequent scans run on the literal payload.
        cmd = r"""gh issue create --title t --body $'## User mandate'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- Round-2 #4: gh api --input - (stdin) fails closed ---

    def test_gh_api_input_stdin_fails_closed(self) -> None:
        # ``gh api ... --input -`` reads the payload from stdin which we
        # cannot inspect from inside the hook. The gate must fail closed.
        cmd = "gh api repos/x/y/issues/1/comments --input -"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high, f"gh api --input - must fail closed via HIGH-matching sentinel; got payload={payload!r}"

    def test_gh_api_input_missing_file_fails_closed(self) -> None:
        # When ``--input`` references a path that does not exist we
        # cannot read the body — fail closed instead of treating it as a
        # clean publish.
        cmd = "gh api repos/x/y/issues/1/comments --input /nonexistent/path.json"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high

    def test_glab_api_input_stdin_fails_closed(self) -> None:
        cmd = "glab api projects/1/issues/1/notes --input -"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high

    # --- Round-2 #5: curl --data=value (equals form) ---

    def test_curl_data_equals_form_is_parsed(self) -> None:
        cmd = 'curl -X POST https://slack.com/api/chat.postMessage --data=\'{"text":"## User mandate\\nship"}\''
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_curl_json_equals_form_is_parsed(self) -> None:
        cmd = 'curl -X POST https://example.com/api/comments --json=\'{"body":"## User mandate\\nship"}\''
        from teatree.hooks.quote_scanner import _extract_bash_payload  # noqa: PLC0415

        body = _extract_bash_payload(cmd)
        assert "User mandate" in body

    def test_curl_data_raw_equals_form_is_parsed(self) -> None:
        cmd = 'curl -X POST https://slack.com/api/chat.postMessage --data-raw=\'{"text":"## User mandate\\nship"}\''
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_curl_data_equals_at_file_fails_closed(self) -> None:
        cmd = "curl -X POST https://slack.com/api/chat.postMessage --data=@some-binary"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high

    # --- Round-2 #6: Slack MCP coverage gaps ---

    @pytest.mark.parametrize(
        ("tool_name", "field"),
        [
            ("mcp__claude_ai_Slack__slack_send_message", "text"),
            ("mcp__claude_ai_Slack__slack_send_message_draft", "text"),
            ("mcp__claude_ai_Slack__slack_schedule_message", "text"),
            ("mcp__claude_ai_Slack__slack_create_canvas", "document_content"),
            ("mcp__claude_ai_Slack__slack_update_canvas", "document_content"),
        ],
    )
    def test_slack_mcp_write_tool_body_is_scanned(self, tool_name: str, field: str) -> None:
        # ``ToolInput`` enumerates a subset of keys for static analysis,
        # but real MCP payloads can carry tool-specific fields like
        # ``document_content`` — cast to the broader shape that the
        # extractor actually accepts.
        from typing import cast  # noqa: PLC0415

        from teatree.hooks.quote_scanner import ToolInput  # noqa: PLC0415

        tool_input = cast("ToolInput", {field: "## User mandate\nship"})
        payload = extract_publish_payload(tool_name, tool_input)
        assert payload is not None
        assert "User mandate" in payload

    def test_slack_create_canvas_with_content_field_is_scanned(self) -> None:
        # Some canvas variants use ``content`` instead of
        # ``document_content``. Both must be picked up.
        from typing import cast  # noqa: PLC0415

        from teatree.hooks.quote_scanner import ToolInput  # noqa: PLC0415

        tool_input = cast("ToolInput", {"content": "## User mandate\nship"})
        payload = extract_publish_payload(
            "mcp__claude_ai_Slack__slack_create_canvas",
            tool_input,
        )
        assert payload is not None
        assert "User mandate" in payload

    @pytest.mark.parametrize(
        "tool_name",
        [
            "mcp__claude_ai_Slack__slack_read_channel",
            "mcp__claude_ai_Slack__slack_read_thread",
            "mcp__claude_ai_Slack__slack_search_public",
            "mcp__claude_ai_Slack__slack_list_channel_members",
        ],
    )
    def test_slack_mcp_read_only_tools_are_not_publish_surfaces(self, tool_name: str) -> None:
        # Read-only Slack tools must NOT trigger the gate — they don't
        # publish anything, and false positives would block legitimate
        # discovery calls.
        assert extract_publish_payload(tool_name, {"text": "## User mandate\nship"}) is None

    def test_slack_schedule_message_high_match_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "tool_name": "mcp__claude_ai_Slack__slack_schedule_message",
            "tool_input": {"text": "## User mandate\nfoo"},
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    # --- Round-2 #7: smart-quote (Unicode) variants ---

    def test_smart_double_quotes_in_blockquote_are_blocked(self) -> None:
        # U+201C / U+201D (left / right double smart quotes) must match
        # the blockquote-attributed HIGH pattern after normalization.
        body = "> “Ship it now.”"
        result = scan_text(body)
        assert result.has_high, f"smart-quoted blockquote must match HIGH; got {result.findings!r}"

    def test_smart_single_quotes_attributed_are_blocked(self) -> None:
        # U+2018 / U+2019 — same handling as straight singles in
        # heading/italic patterns.
        body = "Per user feedback “ship it now”"
        result = scan_text(body)
        assert result.has_high

    def test_low9_and_high_reversed_quotes_are_normalized(self) -> None:
        # U+201A (single low-9), U+201E (double low-9), U+201F
        # (high-reversed-9) — common across CJK/EU typography.
        body = "> „Ship it now.‟"
        result = scan_text(body)
        assert result.has_high

    def test_italic_attributed_smart_quote_is_blocked(self) -> None:
        body = "A direct phrase like _“this is a long enough sentence to trip the gate”_."
        result = scan_text(body)
        assert result.has_high

    def test_smart_quote_in_gh_pr_comment_body_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        body = "> “Ship it now.”"
        cmd = f'gh pr comment 5 -b "{body}"'
        blocked = handle_quote_scanner_pretool(_bash(cmd))
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"


class TestRound3BypassClosures:
    """Regression tests for the round-3 codex-found bypass paths.

    Each test reproduces one finding from the round-3 verdict comment on
    PR #1251. The names align 1:1 with the round-3 finding numbers.
    """

    # --- Round-3 #1: token-internal line continuation ---

    def test_token_internal_line_continuation_in_subcommand_still_parses(self) -> None:
        # ``gh iss\\\nue comment`` — bash REMOVES ``\\\n`` entirely when it
        # is INSIDE a token (the two halves rejoin as ``gh issue comment``).
        # The publish-detection substring match must still see the joined
        # command.
        cmd = 'gh iss\\\nue comment 1 --body "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None, "token-internal \\<NL> must rejoin to a real publish command"
        assert "User mandate" in payload

    def test_token_internal_line_continuation_in_flag_still_parses(self) -> None:
        # ``--bo\\\ndy "x"`` — backslash-newline INSIDE the flag name is
        # eliminated by bash; the joined token is ``--body`` and the body
        # value must still be extracted.
        cmd = 'gh issue comment 1 --bo\\\ndy "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_between_token_line_continuation_still_separates_tokens(self) -> None:
        # ``--body \\\n "x"`` — backslash-newline BETWEEN tokens collapses
        # the whitespace but keeps the two tokens apart.
        cmd = 'gh issue comment 1 --body \\\n  "## User mandate\nship it"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- Round-3 #2: --quote-ok after unspaced metachar ---

    def test_override_after_unspaced_semicolon_is_rejected(self) -> None:
        # ``echo body;echo --quote-ok`` — a real shell tokenizes ``;`` as
        # a separate token regardless of whitespace, so ``--quote-ok``
        # lives in a SECOND command and must not bypass the gate.
        cmd = 'gh issue comment 1 --body "## User mandate\nbody";echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_override_after_unspaced_pipe_is_rejected(self) -> None:
        cmd = 'gh issue comment 1 --body "leak"|echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_override_after_unspaced_double_amp_is_rejected(self) -> None:
        cmd = 'gh issue comment 1 --body "leak"&&echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_override_after_unspaced_double_pipe_is_rejected(self) -> None:
        cmd = 'gh issue comment 1 --body "leak"||echo --quote-ok'
        assert has_quote_ok_override("Bash", {"command": cmd}) is False

    def test_override_after_unspaced_semicolon_blocks_end_to_end(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd = 'gh issue comment 1 --body "## User mandate\nbody";echo --quote-ok'
        blocked = handle_quote_scanner_pretool(_bash(cmd))
        assert blocked is True
        decision = json.loads(capsys.readouterr().out)
        assert decision["permissionDecision"] == "deny"

    # --- Round-3 #3: ANSI-C $'...' with escaped single quote ---

    def test_ansi_c_with_escaped_single_quote_does_not_truncate(self) -> None:
        # ``$'prefix \\'\\n## User mandate\\nship'`` — the escaped single
        # quote inside the ANSI-C body must NOT truncate the value; the
        # full decoded payload (including the heading) must be scanned.
        cmd = r"""gh issue create --title t --body $'prefix \'\n## User mandate\nship'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload, f"escaped single quote must not truncate ANSI-C body; payload={payload!r}"

    def test_ansi_c_with_escaped_double_quote_decodes(self) -> None:
        cmd = r"""gh issue create --title t --body $'\"## User mandate\"\nship'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_ansi_c_unicode_escape_decodes(self) -> None:
        # ``##`` is ``##`` — must decode and trip the heading.
        cmd = r"""gh issue create --title t --body $'## User mandate\nship'"""
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    # --- Round-3 #4: curl -dVALUE attached short-option ---

    def test_curl_d_attached_value_is_parsed(self) -> None:
        # ``-d'{...}'`` — no separator between flag and value. Real curl
        # accepts the attached form per POSIX short-option convention.
        cmd = 'curl -X POST https://slack.com/api/chat.postMessage -d\'{"text":"## User mandate\\nship"}\''
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "User mandate" in payload

    def test_curl_d_attached_at_file_fails_closed(self) -> None:
        # ``-d@path`` (no separator) — fail closed since we cannot read
        # arbitrary attached files.
        cmd = "curl -X POST https://slack.com/api/chat.postMessage -d@some-binary"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high

    # --- Round-3 #5: slack_edit_message MCP tool ---

    def test_slack_edit_message_is_a_publish_surface(self) -> None:
        from typing import cast  # noqa: PLC0415

        from teatree.hooks.quote_scanner import ToolInput  # noqa: PLC0415

        tool_input = cast("ToolInput", {"text": "## User mandate\nship"})
        payload = extract_publish_payload(
            "mcp__claude_ai_Slack__slack_edit_message",
            tool_input,
        )
        assert payload is not None
        assert "User mandate" in payload

    def test_slack_edit_message_high_match_blocks(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = {
            "tool_name": "mcp__claude_ai_Slack__slack_edit_message",
            "tool_input": {"text": "## User mandate\nfoo"},
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        out = json.loads(capsys.readouterr().out)
        assert out["permissionDecision"] == "deny"

    # --- Round-3 #6: gh api -F body= false-positive regression ---

    def test_gh_api_dash_f_body_does_not_false_positive(self) -> None:
        # ``gh api ... -F body="Clean update"`` is a structured field
        # assignment, NOT a file reference. The dedicated
        # ``_API_FIELD_BODY_RE`` must handle it and the git-style
        # ``-F<filename>`` fail-closed must NOT fire on ``gh api`` calls.
        cmd = 'gh api repos/x/y/issues/1/comments -F body="Clean update without quotes"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert not scan.has_high, f"gh api -F body=clean must not fail-closed; findings={scan.findings!r}"
        assert "Clean update" in payload

    def test_glab_api_dash_f_body_does_not_false_positive(self) -> None:
        cmd = 'glab api projects/1/issues/1/notes -F body="Clean note text"'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert not scan.has_high
        assert "Clean note" in payload

    def test_git_commit_dash_f_file_still_fails_closed_when_missing(self, tmp_path: Path) -> None:
        # The git-specific ``-F`` IS a file reference. Round-3 scoping
        # must NOT lose the fail-closed behaviour for git commits.
        cmd = "git commit -F /nonexistent/path.txt"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high


class TestFailClosedSentinelNoSelfMatch:
    """The fail-closed sentinel must not self-match a content pattern (#126)."""

    def test_sentinel_does_not_trip_the_user_said_pattern(self) -> None:
        scan = scan_text(FAIL_CLOSED_SENTINEL)
        # The sentinel is recognised explicitly, never via a content
        # pattern that would describe a body the scanner never saw.
        names = {f.name for f in scan.findings}
        assert names == {"fail-closed-sentinel"}, f"sentinel self-matched a content pattern: {names}"

    def test_missing_dash_f_file_yields_only_the_sentinel_finding(self) -> None:
        cmd = "git commit -F /nonexistent/path-126.txt"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high
        assert {f.name for f in scan.findings} == {"fail-closed-sentinel"}


class TestSentinelProseIsNotAFailClosedMatch:
    """Inert prose that NAMES the sentinel is not the fail-closed condition (#1213).

    The gate exists to block an unresolvable/ambiguous body source — the
    parser injects the sentinel as its own discrete payload fragment for
    that. A commit message or PR body that merely DISCUSSES the gate (and so
    contains the sentinel phrase inside a properly-quoted argument value) is
    not a quoting hazard: the argument is correctly quoted, the phrase is
    documentation. It must pass.
    """

    def test_commit_message_naming_the_sentinel_phrase_is_allowed(self) -> None:
        # A commit whose -m message quotes the full sentinel string as prose.
        body = f"fix(hooks): stop the quote-scanner flagging inert prose that names the {FAIL_CLOSED_SENTINEL} marker"
        cmd = f"git commit -m {body!r}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert not is_fail_closed_sentinel(payload)
        scan = scan_text(payload)
        assert not scan.has_high

    def test_pr_body_naming_the_sentinel_phrase_is_allowed(self) -> None:
        # A gh pr create whose --body documents the gate, quoting the sentinel.
        body = (
            "This PR explains why the scanner fails closed. The injected marker is "
            f"{FAIL_CLOSED_SENTINEL} and the gate refuses the post when it appears."
        )
        cmd = f"gh pr create --title fix --body {body!r}"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert not is_fail_closed_sentinel(payload)
        scan = scan_text(payload)
        assert not scan.has_high

    def test_scan_text_on_mid_line_sentinel_prose_has_no_finding(self) -> None:
        prose = (
            "We document the behaviour: when the scanner cannot resolve a body it "
            f"emits {FAIL_CLOSED_SENTINEL} and blocks. End of explanation."
        )
        assert not is_fail_closed_sentinel(prose)
        scan = scan_text(prose)
        assert {f.name for f in scan.findings} == set()

    def test_genuine_injected_sentinel_still_blocks(self) -> None:
        # The parser injects the sentinel as its own line — that must still fire.
        cmd = "gh api repos/o/r/issues --input -"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert is_fail_closed_sentinel(payload)
        scan = scan_text(payload)
        assert scan.has_high
        assert {f.name for f in scan.findings} == {"fail-closed-sentinel"}

    def test_genuine_sentinel_blocks_even_alongside_a_clean_body_fragment(self) -> None:
        # A clean body on one segment + an unresolvable body on another: the
        # sentinel rides its own line and the gate still fails closed.
        cmd = 'gh issue create --body "a clean note" && gh api repos/o/r/issues --input -'
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert is_fail_closed_sentinel(payload)
        scan = scan_text(payload)
        assert scan.has_high


class TestHeredocToFileDashF:
    """``cat > path <<EOF … EOF; git commit -F path`` resolves the body (#126)."""

    def test_dash_f_resolves_heredoc_written_file_body(self) -> None:
        # At PreToolUse the file does not exist yet (hook runs BEFORE the
        # command), so the only body source is the in-command heredoc.
        cmd = (
            "cat > /tmp/commit-msg-126.txt <<'EOF'\n"
            "refactor: clean up the widget refinery\n"
            "EOF\n"
            "git commit -F /tmp/commit-msg-126.txt"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "clean up the widget refinery" in payload
        # No sentinel — the body was resolved from the heredoc.
        assert not is_fail_closed_sentinel(payload)

    def test_dash_f_heredoc_body_with_user_quote_still_scans_high(self) -> None:
        # The resolved body IS scanned — a verbatim user quote in the
        # heredoc-written commit message still trips the gate.
        cmd = (
            "cat > /tmp/commit-msg-126b.txt <<'EOF'\n"
            "the user said: ship it now\n"
            "EOF\n"
            "git commit -F /tmp/commit-msg-126b.txt"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        scan = scan_text(payload)
        assert scan.has_high
        assert "the-user-said-colon" in {f.name for f in scan.findings}

    def test_dash_f_quoted_redirect_path_matches_bare_reference(self) -> None:
        cmd = (
            "cat > '/tmp/commit msg 126.txt' <<'EOF'\n"
            "refactor: tidy the parser\n"
            "EOF\n"
            "git commit -F '/tmp/commit msg 126.txt'"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "tidy the parser" in payload


class TestHeredocBodyPairing:
    """A file-redirected heredoc is scanned only when its path is posted."""

    def test_unposted_scratch_heredoc_body_is_not_scanned(self) -> None:
        cmd = (
            "cat > /tmp/scratch-pair.txt <<EOF1\n"
            "the user said: this scratch is never posted\n"
            "EOF1\n"
            "cat > /tmp/posted-pair.txt <<EOF2\n"
            "refactor: clean release notes\n"
            "EOF2\n"
            "gh pr create --title t --body-file /tmp/posted-pair.txt"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "clean release notes" in payload
        assert "scratch is never posted" not in payload

    def test_posted_heredoc_path_body_is_scanned(self) -> None:
        cmd = (
            "cat > /tmp/posted-pair-2.txt <<EOF\n"
            "the user said: ship it now\n"
            "EOF\n"
            "gh pr create --title t --body-file /tmp/posted-pair-2.txt"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert scan_text(payload).has_high

    def test_posted_heredoc_path_is_not_double_counted(self) -> None:
        cmd = (
            "cat > /tmp/dedup-pair.txt <<EOF\n"
            "UNIQUEPAYLOADTOKEN once\n"
            "EOF\n"
            "gh pr create --title t --body-file /tmp/dedup-pair.txt"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert payload.count("UNIQUEPAYLOADTOKEN") == 1

    def test_stdin_heredoc_body_is_still_scanned(self) -> None:
        cmd = "gh pr create --title t --body-file - <<EOF\nthe user said: ship it now\nEOF"
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert scan_text(payload).has_high

    def test_unposted_scratch_alongside_stdin_post_only_scans_stdin(self) -> None:
        cmd = (
            "cat > /tmp/scratch-stdin.txt <<EOF1\n"
            "the user said: never posted scratch\n"
            "EOF1\n"
            "gh pr create --title t --body-file - <<EOF2\n"
            "refactor: clean body posted to stdin\n"
            "EOF2"
        )
        payload = extract_publish_payload("Bash", {"command": cmd})
        assert payload is not None
        assert "clean body posted to stdin" in payload
        assert "never posted scratch" not in payload


def _git_init_remote(repo: Path, remote_url: str) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run([git_bin, "init", "-b", "main"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run([git_bin, "remote", "add", "origin", remote_url], cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture
def _private_repo_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text('[teatree]\nprivate_repos = ["acmecorp-engineering"]\n', encoding="utf-8")
    monkeypatch.setenv("T3_BANNED_TERMS_CONFIG", str(cfg))


@pytest.mark.integration
@pytest.mark.usefixtures("_private_repo_cfg")
class TestPrivateRepoCarveOut:
    """A private-repo commit with a verbatim quote downgrades to warn (#126)."""

    def test_private_repo_commit_with_quote_pattern_downgrades_to_warn(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "the user said: ship it now"'},
            "cwd": str(repo),
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False  # downgraded, not denied
        captured = capsys.readouterr()
        assert captured.out == ""  # no deny JSON
        assert "WARNING" in captured.err
        assert _ledger_lines(tmp_path)[-1]["decision"] == "warn-private-repo"

    def test_private_repo_posting_command_with_cwd_target_downgrades(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        # gh issue create (no --repo) from a private CWD resolves the target
        # from the CWD origin and applies the carve-out.
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh issue create --title t --body "the user said: ship it now"'},
            "cwd": str(repo),
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is False  # downgraded, not denied
        captured = capsys.readouterr()
        assert captured.out == ""  # no deny JSON
        assert "WARNING" in captured.err

    def test_explicit_public_repo_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        # An explicit --repo pointing at a public repo must never be carved out.
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {
            "tool_name": "Bash",
            "tool_input": {
                "command": 'gh pr create --repo souliane/teatree --title t --body "the user said: ship it now"'
            },
            "cwd": str(repo),
        }
        blocked = handle_quote_scanner_pretool(data)
        assert blocked is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


@pytest.mark.integration
@pytest.mark.usefixtures("_private_repo_cfg")
class TestUnreadableCommitBodyQuoteGateVisibilityScoped:
    """A readable commit body is resolved+scanned for ALL visibilities; the sentinel is visibility-scoped (#1415/#1213).

    The over-block that stuck multiple coders: a clean ``git commit -F -`` /
    heredoc to the user's own PUBLIC clone hard-blocked via the fail-closed
    sentinel (whose text reads "failing closed" — the "fails open/closed"
    misfire). The fix RESOLVES a readable stdin/heredoc/``printf``-piped body (so
    a real user quote in it still blocks, and a clean one passes) regardless of
    visibility.

    The residual case is a GENUINELY-opaque body (``cat | git commit -F -`` /
    ``-m "$VAR"``) the gate cannot read: the only HIGH finding is the fail-closed
    sentinel. Unlike the banned-terms gate, the quote-scanner has NO push-time
    backstop — ``refuse-public-push-with-leak.sh`` runs ``privacy-scan``, which
    has no verbatim-quote detector — so a verbatim user quote in an opaque body
    would reach public history un-scanned. The sentinel therefore DENIES on a
    PUBLIC commit (as base ``main`` did) and only DOWNGRADES on a provably-PRIVATE
    commit (the #126 carve-out: a private repo cannot leak to the public). Every
    ``gh``/``glab`` POST still hard-blocks an unreadable public body.
    """

    def test_public_commit_heredoc_stdin_clean_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # USED-TO-FALSE-BLOCK, now PASSES: a clean ``git commit -F - <<EOF`` to a
        # public repo. The heredoc body is resolved and scanned clean (no sentinel).
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "git commit -F - <<'EOF'\nfix(gate): the gate fails closed only on a genuinely opaque stdin\nEOF"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is False
        assert capsys.readouterr().out == ""  # clean: no deny JSON

    def test_public_commit_heredoc_stdin_user_quote_still_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: a REAL leaked user quote in the SAME readable heredoc body
        # still hard-blocks. The body is resolved and scanned, so the verbatim-quote
        # pattern fires — the fix removes the unreadable-body false-block, never the
        # real-quote true-block.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        cmd = "git commit -F - <<'EOF'\nthe user said: ship it now without review\nEOF"
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_public_commit_unreadable_var_message_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE FIX (un-backstopped quote leak): ``git commit -m "$VAR"`` whose VAR is
        # not in the hook env is unreadable at scan time, so the only HIGH finding is
        # the fail-closed sentinel. On a PUBLIC commit it DENIES — the quote-scanner
        # has no push-time re-scan (privacy-scan carries no verbatim-quote detector),
        # so a verbatim quote in an opaque body would otherwise reach public history.
        monkeypatch.delenv("UNSET_COMMIT_BODY", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {"tool_name": "Bash", "tool_input": {"command": 'git commit -m "$UNSET_COMMIT_BODY"'}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_public_commit_opaque_cat_pipe_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE OTHER opaque channel: ``cat <file> | git commit -F -``. ``cat`` is not a
        # resolvable ``printf``/``echo`` writer, so the piped body is genuinely opaque
        # at scan time → fail-closed sentinel → DENY on a PUBLIC commit.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@github.com:souliane/teatree.git")
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {"tool_name": "Bash", "tool_input": {"command": "cat draft.txt | git commit -F -"}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_unreadable_var_message_downgrades(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A NON-public equivalent downgrades: the SAME opaque ``-m "$VAR"`` sentinel on
        # a provably-PRIVATE commit downgrades to a warn (#126 — a private repo cannot
        # leak to the public). The coder-unblock for opaque bodies survives where it is
        # provably safe; only the public case reverts to deny.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        data = {"tool_name": "Bash", "tool_input": {"command": 'git commit -m "$UNSET_COMMIT_BODY"'}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is False  # downgraded to warn
        captured = capsys.readouterr()
        assert captured.out == ""  # no deny JSON
        assert "WARNING" in captured.err
        assert _ledger_lines(tmp_path)[-1]["decision"] == "warn-private-repo"

    def test_public_gh_post_unreadable_var_body_still_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-VACUITY: a ``gh`` POST whose ``--body`` is an unreadable ``$VAR`` is the
        # real public action with no push gate behind it, so it STILL hard-blocks.
        monkeypatch.delenv("UNSET_BODY", raising=False)
        monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
        data = {
            "tool_name": "Bash",
            "tool_input": {"command": 'gh pr create --repo souliane/teatree --title t --body "$UNSET_BODY"'},
        }
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"


@pytest.mark.integration
@pytest.mark.usefixtures("_private_repo_cfg")
class TestChainedRawRestPostDefeatsPrivateDowngrade:
    """A private commit chained to a RAW-REST ``gh api`` POST to a PUBLIC repo must DENY (#1213).

    The private-destination downgrade is gated by the chained-segment proof
    ``_chained_segments_provably_inert``. A ``gh api`` POST carries its target in
    the URL PATH (no ``--repo``), so the proof's target resolver falls back to the
    private commit CWD and wrongly accepts the segment as a private post -- a
    verbatim quote in the ``gh api`` body then reaches a PUBLIC repo with the gate
    downgraded to warn. The fix rejects any chained raw-REST segment outright
    (mirroring ``publish_surface._segment_proves_pure_private_post``), so the whole
    command denies. A chained PRIVATE structured post (``gh pr create --repo
    <PRIVATE>``, not raw REST) still downgrades -- the fix does not over-block the
    normal private path.
    """

    def test_private_commit_chained_public_gh_api_post_with_quote_denies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE CONFIRMED LEAK: a clean private commit chained to a public ``gh api``
        # POST whose body is a verbatim user quote. The raw-REST segment must defeat
        # the private downgrade so the quote never reaches the public repo.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        cmd = 'git commit -m clean && gh api repos/souliane/teatree/issues -X POST -f body="the user said: ship it now"'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_sentinel_chained_public_gh_api_post_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The sentinel variant (base ``main`` DENIED this; the private_only delta
        # regressed it to a downgrade): an unreadable ``-m "$VAR"`` commit body chained
        # to a public ``gh api`` POST. The raw-REST guard restores the deny.
        monkeypatch.delenv("UNSET_COMMIT_BODY", raising=False)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        cmd = 'git commit -m "$UNSET_COMMIT_BODY" && gh api repos/souliane/teatree/issues -X POST -f body=acknowledged'
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is True
        assert json.loads(capsys.readouterr().out)["permissionDecision"] == "deny"

    def test_private_commit_chained_private_gh_pr_create_still_downgrades(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ANTI-OVER-BLOCK: a verbatim quote in a private commit chained to a PRIVATE
        # structured ``gh pr create --repo <PRIVATE>`` (NOT raw REST) still downgrades.
        # The raw-REST guard rejects only raw REST, never a normal private post.
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init_remote(repo, "git@gitlab.com:acmecorp-engineering/product.git")
        cmd = (
            'git commit -m "the user said: ship it now" '
            "&& gh pr create --repo acmecorp-engineering/product --title t --body ok"
        )
        data = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}
        assert handle_quote_scanner_pretool(data) is False  # both segments private → downgrade
        assert capsys.readouterr().out == ""  # no deny JSON
        assert _ledger_lines(tmp_path)[-1]["decision"] == "warn-private-repo"


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
        # The escape names the env PREFIX that works on every command, never a
        # ``--quote-ok`` CLI flag a ``t3 review post-comment`` subcommand would
        # reject as an unknown option (#1415).
        assert "QUOTE_OK=1" in message
        assert "--quote-ok" not in message

    def test_warn_message_lists_matched_pattern_names(self) -> None:
        result = ScanResult(findings=[Finding(name="per-user-direction", severity=quote_scanner.MEDIUM, excerpt="x")])
        message = quote_scanner.format_warn_message(result)
        assert "per-user-direction" in message
