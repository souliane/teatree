"""Stop-hook consideration gate for framework-improvement promotion (#1129).

Every session edit must answer "should this be a teatree feature?" before
the turn declares done. The gate scans the transcript for ``Edit`` /
``Write`` / ``NotebookEdit`` tool uses, classifies each modified path
against simple heuristics (P / C / K), and emits a ``BLOCKING REMINDER``
in ``additionalContext`` when one or more paths look like personal
config that the framework should ship.

The classifier is intentionally path-based and conservative — false
positives are downgraded by the agent in the next turn (a justification
in the assistant text or a teatree issue reference is enough to clear
the gate).

Anti-vacuous mutation evidence is built into every match case: the test
both flips a path that SHOULD promote and a path that SHOULDN'T, so
relaxing the pattern in either direction goes RED.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from hooks.scripts.hook_router import _HANDLERS, classify_session_edit, handle_consideration_gate


def _run_hook(payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[bool | None, str]:
    """Invoke the handler with captured stdout, returning ``(rv, stdout)``."""
    buf = io.StringIO()
    monkeypatch.setattr("hooks.scripts.hook_router.sys.stdout", buf)
    rv = handle_consideration_gate(payload)
    return rv, buf.getvalue()


def _write_transcript(tmp_path: Path, entries: list[dict[str, Any]]) -> str:
    """Materialise a Claude-Code transcript JSONL file."""
    path = tmp_path / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


def _assistant_edit(file_path: str, content: str = "x") -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": file_path, "old_string": "a", "new_string": content},
                }
            ],
        },
    }


def _assistant_write(file_path: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": file_path, "content": "x"},
                }
            ],
        },
    }


def _user_turn() -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": "do the thing"}}


def _assistant_text(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


class TestClassifier:
    """Pure-logic unit tests for ``classify_session_edit``."""

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/anyone/.claude/settings.json",
            "/Users/anyone/.claude/hooks.json",
            "/home/dev/.claude/settings.local.json",
            "/Users/anyone/.codex/settings.json",
        ],
    )
    def test_personal_agent_config_promotes(self, path: str) -> None:
        """``~/.claude/settings.json`` and siblings are framework-shaped."""
        assert classify_session_edit(path) == "P"

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/anyone/.claude/CLAUDE.md",
            "/Users/anyone/.codex/AGENTS.md",
        ],
    )
    def test_personal_agent_instructions_promote(self, path: str) -> None:
        """Personal CLAUDE.md / AGENTS.md edits encode behaviour."""
        assert classify_session_edit(path) == "P"

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/anyone/.claude/projects/proj/memory/MEMORY.md",
            "/Users/anyone/.claude/projects/proj/memory/feedback_x.md",
            "/Users/anyone/.claude/todos/active.json",
            "/Users/anyone/.claude/statsig/cache.json",
            "/Users/anyone/.zshrc",
            "/Users/anyone/.bashrc",
            "/Users/anyone/.gitconfig",
            "/Users/anyone/.tmux.conf",
        ],
    )
    def test_genuine_personal_paths_are_keep(self, path: str) -> None:
        """Memory, shell rc, terminal config — keep-personal."""
        assert classify_session_edit(path) == "K"

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/dev/workspace/teatree/src/teatree/loop/tick.py",
            "/Users/dev/workspace/teatree/hooks/scripts/hook_router.py",
            "/Users/dev/workspace/teatree/tests/test_x.py",
            "/Users/dev/workspace/teatree/skills/retro/SKILL.md",
            "/Users/dev/workspace/teatree/pyproject.toml",
            "/tmp/something.txt",
            "",
        ],
    )
    def test_in_repo_and_unrelated_paths_are_none(self, path: str) -> None:
        """Edits to the framework itself, or to unrelated files, are unclassified.

        ``None`` means "nothing to gate on" — the consideration gate only
        speaks up when the path lives in the personal-config corners.
        """
        assert classify_session_edit(path) is None


class TestGate:
    """Integration tests against the real Stop handler."""

    def test_no_transcript_is_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rv, out = _run_hook({"session_id": "s1"}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_no_personal_edits_is_quiet(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Edits to the framework itself never trip the gate."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/workspace/teatree/src/teatree/loop/tick.py"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_promotable_edit_emits_blocking_reminder(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A ``.claude/settings.json`` edit must trigger the gate.

        Anti-vacuous: removing ``.claude/settings.json`` from the promote
        pattern set turns this test RED — the reminder vanishes.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is True
        payload = json.loads(out)
        body = payload["hookSpecificOutput"]["additionalContext"]
        assert "CONSIDERATION GATE" in body
        assert "settings.json" in body
        assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
        # Soft block only — never emit decision: block.
        assert "decision" not in payload

    def test_write_tool_also_counts(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``Write`` is symmetric with ``Edit`` — same gate fires."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_write("/Users/dev/.claude/hooks.json"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is True
        body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "hooks.json" in body

    def test_keep_personal_edit_does_not_trip_gate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Memory writes are explicit personal preference — quiet."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/projects/p/memory/feedback_x.md"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_teatree_issue_reference_clears_gate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Issue reference in assistant text clears the gate.

        Spec's ``open the issue`` half — the gate has nothing left to nag
        about once a ``souliane/teatree#NNNN`` reference is in the turn.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
                _assistant_text("Filed souliane/teatree#1234 to promote this hook into the framework."),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_short_issue_ref_clears_gate(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Bare ``#NNNN`` mention is enough."""
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
                _assistant_text("Tracked as #4321 for framework promotion."),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is None
        assert out == ""

    def test_multiple_promotable_paths_listed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
                _assistant_write("/Users/dev/.claude/hooks.json"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is True
        body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "settings.json" in body
        assert "hooks.json" in body

    def test_stop_hook_active_short_circuits(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """``stop_hook_active`` ⇒ no-op (re-fire guard)."""
        transcript = _write_transcript(
            tmp_path,
            [_user_turn(), _assistant_edit("/Users/dev/.claude/settings.json")],
        )

        rv, out = _run_hook(
            {"session_id": "s1", "transcript_path": transcript, "stop_hook_active": True},
            monkeypatch,
        )

        assert rv is None
        assert out == ""

    def test_only_current_turn_is_scanned(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Edits before the most recent user turn are out of scope.

        Only the current turn's edits count for the Stop decision —
        prior-turn edits already had their gate fire.
        """
        transcript = _write_transcript(
            tmp_path,
            [
                _user_turn(),
                _assistant_edit("/Users/dev/.claude/settings.json"),
                _user_turn(),
                _assistant_edit("/Users/dev/workspace/teatree/src/teatree/loop/tick.py"),
            ],
        )

        rv, out = _run_hook({"session_id": "s1", "transcript_path": transcript}, monkeypatch)

        assert rv is None
        assert out == ""


class TestRouterWiring:
    def test_handler_registered_for_stop(self) -> None:
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert "handle_consideration_gate" in names

    def test_runs_after_answered_questions_gate(self) -> None:
        """Answered-questions and structured-question gates are dominant."""
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert names.index("handle_enforce_answered_questions") < names.index("handle_consideration_gate")

    def test_runs_before_loop_self_pump(self) -> None:
        """The loop self-pump must run last."""
        names = [h.__name__ for h in _HANDLERS["Stop"]]
        assert names.index("handle_consideration_gate") < names.index("handle_loop_self_pump")


class TestCrashProof:
    """The Stop hook must never raise (#810 contract)."""

    def test_handler_swallows_unexpected_errors(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        boom = RuntimeError("simulated transcript read failure")

        def _boom(_path: str) -> list[dict]:
            raise boom

        monkeypatch.setattr("hooks.scripts.hook_router._read_transcript_entries", _boom)

        rv, out = _run_hook({"session_id": "s1", "transcript_path": "/tmp/nope.jsonl"}, monkeypatch)

        assert rv is None
        assert out == ""
        assert "consideration-gate skipped" in capsys.readouterr().err

    def test_corrupt_transcript_is_quiet(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        path = tmp_path / "transcript.jsonl"
        path.write_text("not json\n{also not json\n", encoding="utf-8")

        rv, out = _run_hook({"session_id": "s1", "transcript_path": str(path)}, monkeypatch)

        assert rv is None
        assert out == ""
