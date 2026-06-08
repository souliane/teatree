"""Tests for the closure-verb re-verify advisory (issue #1448).

The orchestrator has claimed a closure ("merged #N", "closed !N", "confirmed
superseded") without verifying the id's live state in the same turn (2x
recurrence). The detector ``closure_reverify_scanner`` and the WARN-only Stop
handler ``handle_closure_reverify_stop`` promote the prose-only
``feedback_done_claims_require_artifact_evidence`` rule to a deterministic
advisory.

WARN-only by design (issue #1448 + the #1567 deadlock precedent): the handler
emits a non-blocking ``systemMessage`` and NEVER denies. The load-bearing
tests are the NO-FIRE cases — a false fire would nag a legitimate or
already-verified closure.

Two layers, both integration-style: the pure detector exercised directly, and
the real ``hook_router`` Stop handler exercised through a real transcript
JSONL written under ``tmp_path`` (only stdin/stdout cross the boundary).
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_closure_reverify_stop
from teatree.hooks import closure_reverify_scanner as scanner


class TestFindClosureClaims:
    """The detector flags an id only when a closure claim sits next to it."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Merged #1448 into main.", ["#1448"]),
            ("PR is now closed: #42.", ["#42"]),
            ("I'll close !6301 — it is superseded.", ["!6301"]),
            ("I've merged PR 99.", ["#99"]),
            ("MR 88 has been merged.", ["!88"]),
            ("Confirmed superseded; closing !777.", ["!777"]),
            ("#5 is resolved.", ["#5"]),
        ],
    )
    def test_closure_claim_is_flagged(self, text: str, expected: list[str]) -> None:
        assert scanner.find_closure_claims(text) == expected

    def test_pr_and_hash_spellings_normalise_to_same_token(self) -> None:
        assert scanner.find_closure_claims("Merged PR 42.") == ["#42"]
        assert scanner.find_closure_claims("Merged MR 42.") == ["!42"]

    def test_multiple_distinct_claims_all_returned_in_order(self) -> None:
        text = "Merged #10 and closed #20."
        assert scanner.find_closure_claims(text) == ["#10", "#20"]

    def test_repeated_id_is_deduplicated(self) -> None:
        assert scanner.find_closure_claims("Merged #10. #10 is closed.") == ["#10"]


class TestNoClosureClaimDoesNotFire:
    """No claim → empty. These keep the detector quiet on benign prose."""

    def test_closure_verb_without_any_id_does_not_fire(self) -> None:
        # "fixed the typo" — a closure verb with no id at all.
        assert scanner.find_closure_claims("Fixed the typo and pushed.") == []

    def test_id_without_closure_verb_does_not_fire(self) -> None:
        assert scanner.find_closure_claims("Working on #1448 now; tests are green.") == []

    def test_closure_verb_far_from_id_does_not_fire(self) -> None:
        # The verb and id are well beyond the proximity window — not one claim.
        text = "Merged the long-running refactor after weeks of review. " + ("x" * 80) + " See #1448 for the tracker."
        assert scanner.find_closure_claims(text) == []

    @pytest.mark.parametrize(
        "text",
        [
            "This builds on the merged #100 from last sprint.",
            "The bug that was fixed in #200 last week resurfaced.",
            "As discussed in #300, the approach holds.",
            "This is a follow-up to #400.",
            "Based on the work landed in #500 previously.",
            "It merged in #600 a while ago, so the API is stable now.",
        ],
    )
    def test_narrative_mention_does_not_fire(self, text: str) -> None:
        assert scanner.find_closure_claims(text) == []


class TestStateCheckedIds:
    """Same-turn state checks bind a read to the specific id it touched."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("gh pr view 1448 --json state", "#1448"),
            ("gh issue view 1448", "#1448"),
            ("glab mr view 1448", "!1448"),
            ("gh api repos/owner/repo/pulls/1448", "#1448"),
            ("t3 merge --pr 1448", "#1448"),
            ("t3 merge --mr 1448", "!1448"),
            ("git log --oneline --grep '#1448'", "#1448"),
        ],
    )
    def test_state_read_with_id_verifies_it(self, command: str, expected: str) -> None:
        assert expected in scanner.state_checked_ids([command])

    def test_read_without_state_verb_does_not_verify(self) -> None:
        # An id appearing in a non-state-read command (e.g. an echo) does not
        # count as verification.
        assert scanner.state_checked_ids(["echo 'see #1448'"]) == set()

    def test_state_read_without_id_verifies_nothing(self) -> None:
        assert scanner.state_checked_ids(["git status"]) == set()

    def test_pr_spelling_in_command_normalises(self) -> None:
        assert "#42" in scanner.state_checked_ids(["gh pr view 42"])


class TestFindUnverifiedClosures:
    """The fire set: claims with NO same-turn state check."""

    def test_claim_without_same_turn_check_fires(self) -> None:
        assert scanner.find_unverified_closures("Merged #1448.", []) == ["#1448"]

    def test_claim_with_same_turn_check_does_not_fire(self) -> None:
        # The agent verified — a gh pr view on the SAME id clears the claim.
        assert scanner.find_unverified_closures("Merged #1448.", ["gh pr view 1448 --json state"]) == []

    def test_only_unverified_claims_remain(self) -> None:
        # !10 was verified (glab mr view 10 → !10); #20 was not — only #20 fires.
        out = scanner.find_unverified_closures("Merged !10 and closed #20.", ["glab mr view 10"])
        assert out == ["#20"]

    def test_check_on_different_id_does_not_clear_the_claim(self) -> None:
        # A state read on an UNRELATED id must not credit the claimed id.
        assert scanner.find_unverified_closures("Merged #1448.", ["gh pr view 999"]) == ["#1448"]

    def test_no_claim_yields_empty(self) -> None:
        assert scanner.find_unverified_closures("Working on #1448.", []) == []


class TestFormatWarnMessage:
    def test_message_lists_ids_and_is_advisory(self) -> None:
        msg = scanner.format_warn_message(["#1448", "!6301"])
        assert "ADVISORY" in msg
        assert "#1448" in msg
        assert "!6301" in msg
        assert "#1448" in msg


def _assistant(text: str, tool_uses: list[dict] | None = None) -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(tool_uses or [])
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _bash(command: str) -> dict:
    return {"type": "tool_use", "name": "Bash", "input": {"command": command}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _output(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


class TestHandlerFires:
    """Anti-vacuous: a high-confidence unverified closure WARNs."""

    def test_unverified_closure_claim_emits_warning(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(
            tmp_path,
            [_user("ship the PR"), _assistant("Done — merged #1448 into main.")],
        )

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        output = _output(capsys)
        assert "ADVISORY" in output.get("systemMessage", "")
        assert "#1448" in output.get("systemMessage", "")
        assert result is True

    def test_warning_buried_in_long_status_fires(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        status = "Loop status:\n- ticket #1 in progress\n- pipeline green\nConfirmed superseded; closing !6301 now."
        transcript = _write_transcript(tmp_path, [_user("run loop"), _assistant(status)])

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert "!6301" in _output(capsys).get("systemMessage", "")
        assert result is True


class TestHandlerDoesNotFire:
    """The load-bearing no-false-fire cases. WARN-only must stay quiet here."""

    def test_same_turn_state_check_suppresses(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # gh pr view on the same id in the same turn — the agent verified.
        transcript = _write_transcript(
            tmp_path,
            [
                _user("close the PR"),
                _assistant("Merged #1448.", tool_uses=[_bash("gh pr view 1448 --json state,mergedAt")]),
            ],
        )

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True

    def test_merge_ceremony_output_does_not_fire(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # The merge-ceremony's own run IS the verification.
        transcript = _write_transcript(
            tmp_path,
            [
                _user("merge it"),
                _assistant("Merged #1448 via the merge ceremony.", tool_uses=[_bash("t3 merge --pr 1448")]),
            ],
        )

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True

    @pytest.mark.parametrize(
        "text",
        [
            "This builds on the merged #100 from last sprint.",
            "The bug that was fixed in #200 last week is back.",
            "As discussed in #300, the approach holds.",
        ],
    )
    def test_narrative_mention_does_not_fire(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user("status"), _assistant(text)])

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True

    def test_closure_verb_with_no_id_does_not_fire(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = _write_transcript(tmp_path, [_user("fix it"), _assistant("Fixed the typo and pushed.")])

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True

    def test_id_mention_without_closure_verb_does_not_fire(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transcript = _write_transcript(tmp_path, [_user("status"), _assistant("Still working on #1448; tests green.")])

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True

    def test_agent_tool_state_check_in_prompt_suppresses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A sub-agent dispatched to verify carries the state read in its prompt.
        agent = {
            "type": "tool_use",
            "name": "Agent",
            "input": {"description": "verify", "prompt": "Run gh pr view 1448 and report the state."},
        }
        transcript = _write_transcript(
            tmp_path,
            [_user("verify and close"), _assistant("Confirmed merged #1448.", tool_uses=[agent])],
        )

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})

        assert _output(capsys) == {}
        assert result is not True


class TestHandlerFailSafe:
    def test_missing_transcript_path_is_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_closure_reverify_stop({}) is not True
        assert _output(capsys) == {}

    def test_nonexistent_transcript_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_closure_reverify_stop({"transcript_path": str(tmp_path / "nope.jsonl")})
        assert result is not True
        assert _output(capsys) == {}

    def test_malformed_transcript_lines_are_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("{not json\n{}\n", encoding="utf-8")
        result = handle_closure_reverify_stop({"transcript_path": str(path)})
        assert result is not True
        assert _output(capsys) == {}

    def test_empty_transcript_is_noop(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("", encoding="utf-8")
        result = handle_closure_reverify_stop({"transcript_path": str(path)})
        assert result is not True
        assert _output(capsys) == {}

    def test_unreadable_transcript_is_noop(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(_assistant("Merged #1448.")) + "\n", encoding="utf-8")

        def _boom(_self: Path, *_args: object, **_kwargs: object) -> str:
            msg = "unreadable"
            raise OSError(msg)

        monkeypatch.setattr(Path, "read_text", _boom)

        result = handle_closure_reverify_stop({"transcript_path": str(path)})
        assert result is not True
        assert _output(capsys) == {}

    def test_scanner_crash_fails_open(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bug in the detector must never wedge turn-end: the handler swallows
        # it and stays silent.
        transcript = _write_transcript(tmp_path, [_user("go"), _assistant("Merged #1448.")])

        def _boom(*_args: object, **_kwargs: object) -> list[str]:
            msg = "detector bug"
            raise RuntimeError(msg)

        monkeypatch.setattr(scanner, "find_unverified_closures", _boom)

        result = handle_closure_reverify_stop({"transcript_path": str(transcript)})
        assert result is not True
        assert _output(capsys) == {}


class TestWiredIntoRouter:
    def test_stop_event_includes_closure_reverify_advisory(self) -> None:
        assert handle_closure_reverify_stop in router._HANDLERS["Stop"]

    def test_runs_before_loop_self_pump(self) -> None:
        # The advisory must win its stdout slot ahead of the loop self-pump,
        # which would otherwise overwrite it with a continuation directive.
        stop_chain = router._HANDLERS["Stop"]
        assert stop_chain.index(handle_closure_reverify_stop) < stop_chain.index(router.handle_loop_self_pump)
