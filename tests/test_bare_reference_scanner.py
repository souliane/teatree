"""Tests for the bare-reference link gate (#1530).

The detection module ``teatree.hooks.bare_reference_scanner`` and its two
handlers — the PreToolUse HARD gate ``handle_bare_reference_pretool`` and
the Stop SOFT warn ``handle_bare_reference_stop`` — together promote the
prose-only "never cite a bare ID, always a clickable link" rule
(``feedback_always_clickable_links_never_bare_ids.md``) to a deterministic
gate, mirroring the #1213 quote-scanner and #1415 banned-terms precedents.

These tests exercise both halves: the pure detector (positives + an
exhaustive anti-false-positive battery) and each handler end-to-end
against a realistic publish surface or transcript.
"""

import json
import sys
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_bare_reference_pretool, handle_bare_reference_stop
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL, extract_title_fragments
from teatree.hooks.bare_reference_scanner import extract_publish_payload, find_bare_references, scan_text


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out.strip()
    return json.loads(raw) if raw else {}


class TestFindBareReferencesPositives:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("fixed #1500", "#1500"),
            ("merged !6301", "!6301"),
            ("see issue #42 for details", "#42"),
            ("MR !7 is green", "!7"),
            ("thread ts 1716900000.123456", "1716900000.123456"),
            ("https://github.com/souliane/teatree/issues/1500", "https://github.com/souliane/teatree/issues/1500"),
            ("https://gitlab.com/group/proj/-/merge_requests/42", "https://gitlab.com/group/proj/-/merge_requests/42"),
            ("https://workspace.notion.site/Some-Page-abc123", "https://workspace.notion.site/Some-Page-abc123"),
        ],
    )
    def test_bare_reference_is_flagged(self, text: str, expected: str) -> None:
        assert expected in find_bare_references(text)

    def test_multiple_bare_refs_all_returned(self) -> None:
        refs = find_bare_references("closes #1500 and #1501, supersedes !42")
        assert "#1500" in refs
        assert "#1501" in refs
        assert "!42" in refs

    def test_trailing_sentence_punctuation_is_stripped_from_url(self) -> None:
        assert find_bare_references("See https://github.com/o/r/pull/9, then merge.") == [
            "https://github.com/o/r/pull/9"
        ]

    def test_only_bare_ref_flagged_when_mixed_with_a_linked_one(self) -> None:
        assert find_bare_references("[text #5 here](http://x) plus bare #6") == ["#6"]

    def test_repeated_bare_ref_is_deduplicated(self) -> None:
        assert find_bare_references("#1500 again #1500 and once more #1500") == ["#1500"]


class TestFindBareReferencesNegatives:
    @pytest.mark.parametrize(
        "text",
        [
            "[#1500](https://github.com/souliane/teatree/issues/1500)",
            "[!6301](https://gitlab.com/g/p/-/merge_requests/6301)",
            "<https://github.com/souliane/teatree/issues/1500|#1500>",
            "<https://github.com/souliane/teatree/issues/1500>",
            "see [the PR](https://github.com/o/r/pull/9)",
        ],
    )
    def test_linked_reference_is_not_flagged(self, text: str) -> None:
        assert find_bare_references(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "merged 5 PRs today",
            "the dataset is 100GB on disk",
            "see line 42 of the file",
            "upgraded to v1.2.3",
            "version 1.2.3 shipped",
            "fixed a bug on line 9000",
            "deadbeef0123 is the sha",
            "abc1234 fixes it",
            "took 30 minutes",
            "port 8080 is open",
            "ticket has 3 subtasks",
            "no references here at all",
            "C#9 targets .NET",
            "channel #general is busy",
            "gh#1500 cross-repo shorthand autolinks",
            "owner/repo#1500 autolinks too",
            "route /api/v2/users#42 fragment",
            "css !important rule",
            "1716900000.1234567 has seven fractional digits",
            "",
        ],
    )
    def test_plain_numbers_and_shas_are_not_flagged(self, text: str) -> None:
        assert find_bare_references(text) == []


class TestExtractPublishPayload:
    def test_gh_issue_create_body(self) -> None:
        payload = extract_publish_payload("Bash", {"command": 'gh issue create --title t --body "see #1500"'})
        assert payload is not None
        assert "#1500" in payload

    def test_git_commit_message(self) -> None:
        payload = extract_publish_payload("Bash", {"command": "git commit -m 'fix: closes #1500'"})
        assert payload is not None
        assert "#1500" in payload

    def test_non_publish_command_returns_none(self) -> None:
        assert extract_publish_payload("Bash", {"command": "ls -la"}) is None

    def test_slack_mcp_send_message_body(self) -> None:
        payload = extract_publish_payload("mcp__claude_ai_Slack__slack_send_message", {"text": "merged !6301 just now"})
        assert payload is not None
        assert "!6301" in payload

    def test_slack_canvas_document_content_body(self) -> None:
        payload = extract_publish_payload(
            "mcp__claude_ai_Slack__slack_create_canvas", {"document_content": "tracking #1500"}
        )
        assert payload == "tracking #1500"

    def test_slack_read_tool_returns_none(self) -> None:
        assert extract_publish_payload("mcp__claude_ai_Slack__slack_read_channel", {"text": "see #1500"}) is None

    def test_slack_write_tool_without_body_returns_empty(self) -> None:
        assert extract_publish_payload("mcp__claude_ai_Slack__slack_send_message", {}) == ""

    def test_non_slack_mcp_tool_returns_none(self) -> None:
        assert extract_publish_payload("mcp__claude_ai_Notion__notion-update-page", {"text": "see #1500"}) is None


class TestScanText:
    def test_bare_ref_body_has_findings(self) -> None:
        assert scan_text("see #1500") == ["#1500"]

    def test_linked_body_is_clean(self) -> None:
        assert scan_text("[#1500](https://github.com/o/r/issues/1500)") == []

    def test_unparseable_body_fails_closed(self) -> None:
        assert scan_text(FAIL_CLOSED_SENTINEL) != []

    def test_empty_body_is_clean(self) -> None:
        assert scan_text("") == []


class TestPreToolUseHardGate:
    def test_bare_ref_in_gh_issue_body_is_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('gh issue create --title t --body "see #1500"'))
        assert blocked is True
        decision = _out(capsys)
        assert decision["permissionDecision"] == "deny"
        assert "#1500" in decision["permissionDecisionReason"]
        assert "clickable link" in decision["permissionDecisionReason"]

    def test_linked_ref_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = 'gh issue create --title t --body "see [#1500](https://github.com/o/r/issues/1500)"'
        blocked = handle_bare_reference_pretool(_bash(cmd))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_non_publish_command_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("ls -la"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_unparseable_body_fails_closed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("gh api repos/o/r/issues --input -"))
        assert blocked is True
        assert _out(capsys)["permissionDecision"] == "deny"

    def test_clean_body_with_plain_numbers_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('gh pr create --title t --body "merged 5 PRs, 100GB freed"'))
        assert blocked is False
        assert capsys.readouterr().out == ""


class TestExtractTitleFragments:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ('gh pr create --title "feat: x" --body "b"', ["feat: x"]),
            ("gh issue create --title=feat:x", ["feat:x"]),
            ("gh pr create -t feat:x", ["feat:x"]),
            ("gh pr create -tfeat:x", ["feat:x"]),
            ('glab mr create --title "fix: y"', ["fix: y"]),
            ("git commit --message 'sub' ", ["sub"]),
            ("git commit --message=sub", ["sub"]),
            ("git commit -msub", ["sub"]),
            ("git commit -m 'subject\nbody line'", ["subject"]),
        ],
    )
    def test_title_or_subject_fragment_extracted(self, command: str, expected: list[str]) -> None:
        assert extract_title_fragments(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            'gh pr create --body "no title here"',
            "git commit --amend --no-edit",
            "ls -la",
        ],
    )
    def test_no_title_fragment(self, command: str) -> None:
        assert extract_title_fragments(command) == []


class TestConventionalTitleSuffixExemption:
    """The trailing ``(#NNNN)``/``(!NNNN)`` of a PR/MR title or commit subject is exempt.

    The forge auto-links the ref there and the suffix is the universal
    convention, so it is allowed. The exemption is narrow: bodies, slack,
    and mid-title refs stay flagged.
    """

    def test_gh_pr_title_trailing_suffix_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('gh pr create --title "feat(x): desc (#123)" --body "ok"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_glab_mr_title_trailing_suffix_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('glab mr create --title "fix(y): z (!45)" --description "ok"'))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_subject_trailing_suffix_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("git commit -m 'fix(y): z (#45)'"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_subject_double_trailing_suffix_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("git commit -m 'feat: x (#1530) (#1535)'"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_git_commit_subject_triple_trailing_suffix_is_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("git commit -m 'feat: y (#10) (#20) (#30)'"))
        assert blocked is False
        assert capsys.readouterr().out == ""

    def test_bare_ref_in_pr_body_is_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('gh pr create --title "feat(x): desc (#123)" --body "see #99"'))
        assert blocked is True
        assert "#99" in _out(capsys)["permissionDecisionReason"]

    def test_bare_ref_in_slack_send_is_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(
            {"tool_name": "mcp__claude_ai_Slack__slack_send_message", "tool_input": {"text": "merged !45 and #123"}}
        )
        assert blocked is True
        reason = _out(capsys)["permissionDecisionReason"]
        assert "!45" in reason
        assert "#123" in reason

    def test_mid_title_ref_is_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash('gh pr create --title "fixes #123 in the parser" --body "ok"'))
        assert blocked is True
        assert "#123" in _out(capsys)["permissionDecisionReason"]

    def test_mid_subject_ref_with_trailing_suffix_is_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        blocked = handle_bare_reference_pretool(_bash("git commit -m 'feat: see #99 (#45)'"))
        assert blocked is True
        reason = _out(capsys)["permissionDecisionReason"]
        assert "#99" in reason
        assert "#45" not in reason

    def test_git_commit_body_ref_is_still_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        cmd = "git commit -m 'fix(y): z (#45)' -m 'follow-up to #99'"
        blocked = handle_bare_reference_pretool(_bash(cmd))
        assert blocked is True
        reason = _out(capsys)["permissionDecisionReason"]
        assert "#99" in reason
        assert "#45" not in reason

    def test_extract_strips_only_trailing_title_suffix(self) -> None:
        payload = extract_publish_payload(
            "Bash", {"command": 'gh pr create --title "feat(x): desc (#123)" --body "body #99"'}
        )
        assert payload is not None
        assert "#123" not in payload
        assert "#99" in payload

    def test_strip_targets_title_line_not_body_substring_copy(self) -> None:
        command = 'gh pr create --body "ref: feat(x): desc (#123) and more" --title "feat(x): desc (#123)"'
        payload = extract_publish_payload("Bash", {"command": command})
        assert payload is not None
        assert "ref: feat(x): desc (#123) and more" in payload
        assert "feat(x): desc\n" in payload or payload.endswith("feat(x): desc")


class TestStopSoftWarn:
    def test_bare_ref_in_assistant_text_warns_not_denies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user("ship it"), _assistant("Done, this closes #1500.")])
        result = handle_bare_reference_stop({"transcript_path": str(transcript)})
        out = _out(capsys)
        assert "systemMessage" in out
        assert "#1500" in out["systemMessage"]
        assert "decision" not in out
        assert result is True

    def test_linked_assistant_text_is_silent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(
            tmp_path,
            [_user("ship it"), _assistant("Done, [#1500](https://github.com/o/r/issues/1500) is merged.")],
        )
        result = handle_bare_reference_stop({"transcript_path": str(transcript)})
        assert result is None
        assert capsys.readouterr().out == ""

    def test_plain_numbers_in_assistant_text_are_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user("status"), _assistant("Merged 5 PRs, freed 100GB.")])
        result = handle_bare_reference_stop({"transcript_path": str(transcript)})
        assert result is None
        assert capsys.readouterr().out == ""

    def test_missing_transcript_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_bare_reference_stop({"transcript_path": ""})
        assert result is None
        assert capsys.readouterr().out == ""


class TestHandlersFailOpenWithoutTeatreeImport:
    """A failure to import ``teatree`` (or any internal error) must fail open.

    The hook runs in the user's session shell with no guarantee that
    ``teatree`` is importable (#1314). The PreToolUse gate returns
    ``False`` (no block) and the Stop gate returns ``None`` (no warn),
    each silently.
    """

    @pytest.fixture
    def _teatree_unimportable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for mod in ("teatree.hooks.bare_reference_scanner", "teatree.hooks", "teatree"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.setitem(sys.modules, "teatree", None)

    @pytest.mark.usefixtures("_teatree_unimportable")
    def test_pretool_returns_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_bare_reference_pretool(_bash("gh issue create --body x")) is False
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    @pytest.mark.usefixtures("_teatree_unimportable")
    def test_stop_returns_none(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user("go"), _assistant("closes #1500")])
        assert handle_bare_reference_stop({"transcript_path": str(transcript)}) is None
        assert capsys.readouterr().out == ""

    def test_pretool_non_dict_tool_input_is_a_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_bare_reference_pretool({"tool_name": "Bash", "tool_input": "not-a-dict"}) is False
        assert capsys.readouterr().out == ""


class TestHookChainRegistration:
    def test_pretool_handler_runs_before_quote_scanner(self) -> None:
        names = [h.__name__ for h in router._HANDLERS["PreToolUse"]]
        assert "handle_bare_reference_pretool" in names
        assert names.index("handle_bare_reference_pretool") < names.index("handle_quote_scanner_pretool")

    def test_stop_handler_runs_before_consideration_gate(self) -> None:
        names = [h.__name__ for h in router._HANDLERS["Stop"]]
        assert "handle_bare_reference_stop" in names
        assert names.index("handle_bare_reference_stop") < names.index("handle_consideration_gate")
