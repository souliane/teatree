# test-path: cross-cutting
# Exercises the hooks/scripts/standing_goal_stop_gate.py Stop gate together with
# teatree.core.models.StandingGoal — it spans the hook leaf and the ORM.
"""Tests for the standing verified-green Stop gate (PR-25, M8).

While an active :class:`~teatree.core.models.standing_goal.StandingGoal` is unmet,
the Stop gate DENIES a stop-as-if-done (``{"decision": "block", ...}``), leading
with the blunt binary and naming an audited per-action escape. A passing check
auto-retires the goal. Never-lockout: attended turns are skipped, the kill-switch
and the audited token both allow, and any crash fails open.

Integration-style: the real gate, real StandingGoal rows, real ``true``/``false``
check commands; only stdout and the loop-driver verdict cross the boundary.
"""

import json
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import hooks.scripts.hook_router as router
import hooks.scripts.standing_goal_stop_gate as gate
from teatree.core.models import StandingGoal
from tests._gate_escape_audit import audited_escape


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _user(text: str = "go") -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


class _GateTest(TestCase):
    """Isolated STATE_DIR + forced loop-driver verdict; a stdout-capturing gate runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.enterContext(patch.object(router, "STATE_DIR", self.tmp_path))
        self.enterContext(patch.object(router, "_session_drives_loop", lambda _session_id: True))

    def _transcript(self, text: str) -> str:
        path = self.tmp_path / "transcript.jsonl"
        body = "\n".join(json.dumps(e) for e in (_user("deliver"), _assistant(text)))
        path.write_text(body + "\n", encoding="utf-8")
        return str(path)

    def _run(
        self, text: str = "made good progress, wrapping up.", *, session_id: str = "s-goal", extra: dict | None = None
    ) -> tuple[bool | None, dict]:
        data: dict = {"session_id": session_id, "transcript_path": self._transcript(text)}
        if extra:
            data.update(extra)
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = gate.handle_standing_goal_stop(data)
        out = buf.getvalue().strip()
        return result, (json.loads(out) if out else {})


class TestDeniesUnmetGoal(_GateTest):
    def test_unmet_goal_blocks_the_stop(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")

        result, decision = self._run()

        assert result is True
        assert decision.get("decision") == "block"
        reason = decision.get("reason", "")
        assert "`evals-green` green? NO." in reason
        assert "[gate-escape:standing_goal_stop:" in reason


class TestAllowsWhenMetOrOutOfScope(_GateTest):
    def test_no_goals_allows(self) -> None:
        result, decision = self._run()
        assert result is None
        assert decision == {}

    def test_passing_check_allows_and_retires_the_goal(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "true")

        result, decision = self._run()

        assert result is None
        assert decision == {}
        assert StandingGoal.objects.get(name="evals-green").active is False

    def test_attended_non_driver_turn_is_skipped(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")
        with patch.object(router, "_session_drives_loop", lambda _session_id: False):
            result, decision = self._run()
        assert result is None
        assert decision == {}

    def test_stop_hook_active_reentry_is_skipped(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")
        result, decision = self._run(extra={"stop_hook_active": True})
        assert result is None
        assert decision == {}

    def test_kill_switch_allows(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")
        with patch.object(gate, "_gate_enabled", lambda: False):
            result, decision = self._run()
        assert result is None
        assert decision == {}

    def test_errored_check_fails_open(self) -> None:
        # A check that cannot be evaluated (timeout/exception) must NOT deny.
        StandingGoal.objects.set_goal("evals-green", "sleep 999")
        with patch.object(gate, "_evaluate_goal", lambda _cmd: "error"):
            result, decision = self._run()
        assert result is None
        assert decision == {}


class TestCrashProof(_GateTest):
    def test_internal_error_fails_open(self) -> None:
        def _boom(_data: dict) -> bool | None:
            raise RuntimeError

        with patch.object(gate, "_run", _boom):
            result, decision = self._run()
        assert result is None
        assert decision == {}

    def test_unbootstrappable_django_allows(self) -> None:
        # ``_run`` lazily does ``from hooks.scripts.django_bootstrap import
        # bootstrap_teatree_django`` (the canonical package module) — patch it there.
        StandingGoal.objects.set_goal("evals-green", "false")
        with patch.object(sys.modules["hooks.scripts.django_bootstrap"], "bootstrap_teatree_django", lambda: False):
            result, decision = self._run()
        assert result is None
        assert decision == {}


class TestDefensiveHelpers(_GateTest):
    """Cover the fail-open / crash-proof branches directly (a Stop hook never crashes)."""

    def test_evaluate_goal_verdicts(self) -> None:
        assert gate._evaluate_goal("true") == "pass"
        assert gate._evaluate_goal("false") == "fail"

    def test_evaluate_goal_exception_is_error(self) -> None:
        def _raise(*_a: object, **_k: object) -> None:
            raise OSError

        with patch.object(gate.subprocess, "run", _raise):
            assert gate._evaluate_goal("true") == "error"

    def test_retire_goal_swallows_errors(self) -> None:
        def _raise(_name: str) -> bool:
            raise RuntimeError

        with patch.object(StandingGoal.objects, "retire", _raise):
            gate._retire_goal("evals-green")  # must not raise

    def test_check_cache_corrupt_is_empty_and_save_survives_errors(self) -> None:
        (self.tmp_path / "global.standing-goal-check-cache").write_text("not json", encoding="utf-8")
        assert gate._load_check_cache() == {}

        def _raise(*_a: object, **_k: object) -> None:
            raise OSError

        with patch.object(gate.Path, "write_text", _raise):
            gate._save_check_cache({"ts": 0.0, "results": {}})  # must not raise

    def test_check_cache_non_dict_is_empty(self) -> None:
        (self.tmp_path / "global.standing-goal-check-cache").write_text("[1]", encoding="utf-8")
        assert gate._load_check_cache() == {}


class TestAuditedStandingGoalEscape(_GateTest):
    def test_legacy_hold_token_no_longer_allows(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")
        result, blocked = self._run(text="blocked [standing-goal-hold: old-token infra]")
        assert result is True
        assert blocked["decision"] == "block"

    def test_audited_escape_is_recorded_for_this_session_then_allows_the_stop(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "false")
        with audited_escape({}, event="Stop") as requests:
            result, decision = self._run(text="blocked [gate-escape:standing_goal_stop: infra]", extra={"cwd": "/work"})
        assert result is None
        assert decision == {}
        assert [(request["gate"], request["session_id"]) for request in requests] == [("standing_goal_stop", "s-goal")]
