"""Tests for the ``handle_route_away_mode_question`` PreToolUse hook (#58).

Integration-first: the real ``hook_router`` handler is invoked with a
PreToolUse payload synthesised in-process, and the assertion is on
the JSON stdout + the ``DeferredQuestion`` row that landed in the
test DB. The load-bearing §807 interop test is at the bottom:
synthesising a transcript with a hook-converted ``AskUserQuestion``
tool_use and asserting the structured-question Stop gate then
returns ``None`` (gate satisfied — the call is *structurally
complete*, just converted at the PreToolUse layer).
"""

import json
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_enforce_structured_question, handle_route_away_mode_question
from teatree.core.models.deferred_question import DeferredQuestion

pytestmark = pytest.mark.django_db


def _ask_payload(question: str, options: list[dict] | None = None, **extra: str) -> dict:
    payload: dict = {
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {"question": question, "options": options or []},
            ],
        },
    }
    payload.update(extra)
    return payload


def _stdout(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


@pytest.fixture(autouse=True)
def _force_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """All tests in this module exercise the away-mode branch.

    The mode resolver normally reads from disk; for these unit-tests
    we force the resolver result so the hook is exercised under a
    deterministic state without touching the user's real config.
    """
    monkeypatch.setattr(router, "_resolved_away_mode", lambda: True)


class TestAwayModeConversion:
    def test_records_deferred_question_and_emits_deny(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question(_ask_payload("Should I ship?"))
        assert result is True
        out = _stdout(capsys)
        assert out["permissionDecision"] == "deny"
        assert "DeferredQuestion" in out["permissionDecisionReason"]
        rows = list(DeferredQuestion.objects.all())
        assert len(rows) == 1
        assert rows[0].question == "Should I ship?"
        assert rows[0].is_pending is True

    def test_captures_session_and_tool_use_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_route_away_mode_question(
            _ask_payload(
                "X or Y?",
                options=[{"label": "X"}, {"label": "Y"}],
                session_id="sess-42",
                tool_use_id="toolu_42",
            )
        )
        capsys.readouterr()  # drain
        row = DeferredQuestion.objects.get(question="X or Y?")
        assert row.session_id == "sess-42"
        assert row.tool_use_id == "toolu_42"
        assert json.loads(row.options_json) == [{"label": "X"}, {"label": "Y"}]

    def test_reason_names_the_recorded_row_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_route_away_mode_question(_ask_payload("How?"))
        out = _stdout(capsys)
        row = DeferredQuestion.objects.latest("created_at")
        assert f"#{row.pk}" in out["permissionDecisionReason"]
        assert f"answer {row.pk}" in out["permissionDecisionReason"]

    def test_empty_question_fails_open(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question(_ask_payload(""))
        assert result is False
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0

    def test_non_askuserquestion_tool_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = handle_route_away_mode_question({"tool_name": "Bash", "tool_input": {}})
        assert result is False
        assert _stdout(capsys) == {}


class TestPresentModeDoesNotIntercept:
    def test_present_mode_skips_the_handler(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(router, "_resolved_away_mode", lambda: False)
        result = handle_route_away_mode_question(_ask_payload("Should I ship?"))
        assert result is False
        assert _stdout(capsys) == {}
        assert DeferredQuestion.objects.count() == 0


class TestSection807InteropGate:
    """The load-bearing §807 interop test.

    BLUEPRINT §17.1 invariant 9 promises that the away-mode path is a
    *sanctioned destination* for the same ``AskUserQuestion`` tool call
    — converted at the ``PreToolUse`` layer — never an inline prose
    fallback. A converted call still emits a ``tool_use`` block in the
    transcript (the PreToolUse deny denies *execution* but the tool_use
    itself is recorded). The §807 ``handle_enforce_structured_question``
    Stop gate reads the transcript's last assistant turn, sees that a
    ``AskUserQuestion`` tool_use occurred, and returns ``None`` —
    indicating the structured-question gate is satisfied.
    """

    def _transcript_with_tool_use(self, tmp_path: Path) -> Path:
        path = tmp_path / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Should I proceed? Recording for later."},
                        {"type": "tool_use", "name": "AskUserQuestion", "input": {}},
                    ],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return path

    def test_converted_question_satisfies_807_gate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        transcript = self._transcript_with_tool_use(tmp_path)
        result = handle_enforce_structured_question({"transcript_path": str(transcript)})
        assert result is None
        # No 'block' decision was written.
        out = capsys.readouterr().out.strip()
        assert out == ""

    def test_inline_question_without_tool_use_still_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sanity: §807 gate still fires when there is no tool call.

        Without this assertion the previous test could be passing
        because the §807 gate is broken in general — we want to prove
        it is the tool_use block specifically that satisfies the gate.
        """
        path = tmp_path / "transcript.jsonl"
        entries = [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Should I proceed? Please choose A or B."},
                    ],
                },
            },
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        result = handle_enforce_structured_question({"transcript_path": str(path)})
        assert result is True
