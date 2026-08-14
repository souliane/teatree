# test-path: cross-cutting — a hooks/scripts gate module; no src/teatree/ mirror.
"""The dispatch brief is linted for unanchored factual assertions (#4341).

Writing the behavioural rule down failed twice, so this is the enforcement arm: at
the ``PreToolUse`` ``Agent``/``Task`` interception point, a brief that asserts
verifiable specifics with no SHA anchor and no trust-the-code clause gets one
warning naming the remedy.

It WARNS by default and never denies — the fleet dispatches constantly, and a
false deny on an ordinary brief is far costlier than an ignored line. The refuse
posture is opt-in behind ``brief_anchor_gate_refuse``; these pin both, plus the
never-lockout escapes the deny arm inherits.
"""

import pytest

import hooks.scripts.brief_anchor_gate as gate
import hooks.scripts.hook_router as router
from teatree.hooks import brief_anchor_scanner as scanner

_UNANCHORED = "The guard is in src/teatree/core/ticket.py:412 and there are 3 callers. Fix each."
_ANCHORED = f"{_UNANCHORED} {scanner.TRUST_THE_CODE_CLAUSE}"
_GATE_BUG = "gate bug"


@pytest.fixture(autouse=True)
def _enabled_warn_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default posture: the gate is on and warns."""
    monkeypatch.setattr(gate, "_gate_enabled", lambda: True)
    monkeypatch.setattr(gate, "_refuse_mode", lambda: False)


def _dispatch(prompt: str = _UNANCHORED, **extra: object) -> dict:
    return {"tool_name": "Agent", "tool_input": {"prompt": prompt}, **extra}


class TestWarnPosture:
    def test_an_unanchored_brief_warns_without_denying(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert gate.handle_brief_anchor_lint(_dispatch()) is False
        assert "BRIEF-ANCHOR LINT" in capsys.readouterr().err

    def test_the_warning_quotes_the_remedy_clause(self, capsys: pytest.CaptureFixture[str]) -> None:
        gate.handle_brief_anchor_lint(_dispatch())
        assert scanner.TRUST_THE_CODE_CLAUSE in capsys.readouterr().err

    def test_a_brief_carrying_the_clause_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert gate.handle_brief_anchor_lint(_dispatch(_ANCHORED)) is False
        assert capsys.readouterr().err == ""

    def test_the_task_dispatch_tool_is_linted_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        gate.handle_brief_anchor_lint({"tool_name": "Task", "tool_input": {"prompt": _UNANCHORED}})
        assert "BRIEF-ANCHOR LINT" in capsys.readouterr().err

    def test_a_non_dispatch_tool_is_untouched(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert gate.handle_brief_anchor_lint({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is False
        assert capsys.readouterr().err == ""


class TestRefusePosture:
    def test_refuse_mode_denies_an_unanchored_brief(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        assert gate.handle_brief_anchor_lint(_dispatch()) is True
        assert "BRIEF-ANCHOR LINT" in capsys.readouterr().out

    def test_refuse_mode_still_allows_an_anchored_brief(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        assert gate.handle_brief_anchor_lint(_dispatch(_ANCHORED)) is False

    def test_the_deny_routes_through_the_fail_open_chokepoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", lambda: True)
        assert gate.handle_brief_anchor_lint(_dispatch()) is False


class TestNeverLockout:
    def test_the_kill_switch_disables_the_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "_gate_enabled", lambda: False)
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        assert gate.handle_brief_anchor_lint(_dispatch()) is False
        assert capsys.readouterr().err == ""

    def test_the_escape_token_clears_one_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        data = _dispatch(f"[brief-anchor-ok: the sha is in the ticket] {_UNANCHORED}")
        assert gate.handle_brief_anchor_lint(data) is False
        assert capsys.readouterr().err == ""

    def test_an_empty_escape_reason_does_not_clear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_refuse_mode", lambda: True)
        assert gate.handle_brief_anchor_lint(_dispatch(f"[brief-anchor-ok: ] {_UNANCHORED}")) is True

    def test_an_internal_error_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _explode() -> bool:
            raise RuntimeError(_GATE_BUG)

        monkeypatch.setattr(gate, "_gate_enabled", _explode)
        assert gate.handle_brief_anchor_lint(_dispatch()) is False

    def test_a_malformed_payload_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert gate.handle_brief_anchor_lint({"tool_name": "Agent", "tool_input": "not-a-dict"}) is False
        assert gate.handle_brief_anchor_lint({"tool_name": "Agent", "tool_input": {"prompt": 7}}) is False
        assert capsys.readouterr().err == ""


class TestRegistration:
    def test_the_lint_is_registered_on_the_dispatch_arm(self) -> None:
        assert gate.handle_brief_anchor_lint in router._HANDLERS["PreToolUse"]
