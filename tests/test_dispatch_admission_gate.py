# test-path: cross-cutting — a hooks/scripts gate module; no src/teatree/ mirror.
"""The interactive dispatch consults the admission governor (#4107).

The governor governed the headless population only: an ``Agent``/``Task``
dispatch from an orchestrator session was admitted unconditionally, so the two
agent populations summed unchecked. These cover the hook arms that close it —
the ``PreToolUse`` ``Agent``/``Task`` gate and its ``TaskCreated`` fan-out
counterpart — plus the never-lockout escapes each ships with.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import hooks.scripts.dispatch_admission_gate as gate
import hooks.scripts.django_bootstrap as bootstrap
import hooks.scripts.hook_router as router
from teatree.core import dispatch_admission as core

_BRAKE = "load 58 at/over the 40 watermark on 8 core(s)"
_PLUGIN_ROOT = Path(router.__file__).resolve().parents[2]
#: Bound before the autouse stub replaces it, so the seam's own tests reach the real one.
_REAL_DENIED_REASON = gate._denied_reason


@pytest.fixture(autouse=True)
def _governor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: the governor is silent, so only a test that brakes sees a deny."""
    monkeypatch.setattr(gate, "_denied_reason", lambda *, apply_ceiling, session_id="": None)


def _braking(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Make the governor brake, recording each call's ``apply_ceiling``."""
    seen: list[bool] = []

    def _reason(*, apply_ceiling: bool, session_id: str = "") -> str:
        seen.append(apply_ceiling)
        return _BRAKE

    monkeypatch.setattr(gate, "_denied_reason", _reason)
    return seen


_PROBE_EXPLODED = "probe exploded"


def _exploding_probe(*, apply_ceiling: bool, session_id: str = "") -> str:
    raise RuntimeError(_PROBE_EXPLODED)


def _agent_dispatch(prompt: str = "review the diff", **extra: object) -> dict:
    return {"tool_name": "Agent", "tool_input": {"prompt": prompt}, **extra}


class TestPreToolUseArm:
    def test_a_brake_denies_and_names_it(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        _braking(monkeypatch)
        assert gate.handle_dispatch_admission(_agent_dispatch()) is True
        assert _BRAKE in capsys.readouterr().out

    def test_a_healthy_governor_allows(self) -> None:
        assert gate.handle_dispatch_admission(_agent_dispatch()) is False

    def test_a_non_dispatch_tool_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _braking(monkeypatch)
        assert gate.handle_dispatch_admission({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is False
        assert seen == [], "the governor must not be probed for a non-dispatch tool"

    def test_the_task_tool_is_governed_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _braking(monkeypatch)
        assert gate.handle_dispatch_admission({"tool_name": "Task", "tool_input": {"prompt": "x"}}) is True

    def test_the_escape_token_allows_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _braking(monkeypatch)
        data = _agent_dispatch("[admission-ok: cold review is the merge blocker] review the diff")
        assert gate.handle_dispatch_admission(data) is False
        assert seen == [], "the token short-circuits before the probe"

    def test_an_empty_escape_reason_does_not_allow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _braking(monkeypatch)
        assert gate.handle_dispatch_admission(_agent_dispatch("[admission-ok: ] go")) is True

    def test_a_malformed_payload_is_still_governed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-dict tool_input / non-str prompt carries no token, so it cannot
        # short-circuit — the governor is still asked.
        seen = _braking(monkeypatch)
        assert gate.handle_dispatch_admission({"tool_name": "Agent", "tool_input": "not-a-dict"}) is True
        assert gate.handle_dispatch_admission({"tool_name": "Agent", "tool_input": {"prompt": 7}}) is True
        assert seen == [True, True]

    def test_a_subagent_dispatch_skips_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Its own lane already admitted it; re-clamping against a ceiling its own
        # claim is counted in would deadlock it against itself.
        seen = _braking(monkeypatch)
        gate.handle_dispatch_admission(_agent_dispatch(agent_id="a-123"))
        assert seen == [False]

    def test_a_main_agent_dispatch_applies_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _braking(monkeypatch)
        gate.handle_dispatch_admission(_agent_dispatch())
        assert seen == [True]

    def test_the_dispatchs_session_reaches_the_seat_ledger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # #4129: the seat is booked under the dispatching session, so a release can find it.
        seen: list[str] = []
        monkeypatch.setattr(
            gate,
            "_denied_reason",
            lambda *, apply_ceiling, session_id="": seen.append(session_id) or None,
        )
        gate.handle_dispatch_admission(_agent_dispatch(session_id="sess-4129"))
        assert seen == ["sess-4129"]

    def test_an_internal_error_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "_denied_reason", _exploding_probe)
        assert gate.handle_dispatch_admission(_agent_dispatch()) is False

    def test_the_deny_routes_through_the_fail_open_chokepoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The master danger_gate_fail_open switch must relax it like every other
        # over-deny gate (the never-lockout contract, #2349).
        _braking(monkeypatch)
        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", lambda: True)
        assert gate.handle_dispatch_admission(_agent_dispatch()) is False


class TestTaskCreatedArm:
    def _task(self, subject: str = "implement the fix", description: str = "") -> dict:
        return {
            "session_id": "sess-4107",
            "task_id": "t-1",
            "task_subject": subject,
            "task_description": description,
        }

    def test_a_brake_denies_the_fanned_out_task(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        _braking(monkeypatch)
        assert gate.handle_dispatch_admission_on_task_create(self._task()) is True
        assert _BRAKE in capsys.readouterr().out

    def test_a_healthy_governor_allows(self) -> None:
        assert gate.handle_dispatch_admission_on_task_create(self._task()) is False

    def test_a_missing_session_id_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _braking(monkeypatch)
        assert gate.handle_dispatch_admission_on_task_create({"task_subject": "x"}) is False
        assert seen == []

    def test_the_escape_token_allows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _braking(monkeypatch)
        data = self._task(description="[admission-ok: the merge keystone is blocked on this]")
        assert gate.handle_dispatch_admission_on_task_create(data) is False

    def test_the_fan_out_never_applies_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The TaskCreated schema carries no agent_id, so origin is unknowable —
        # and unknown never brakes on a count it cannot attribute.
        seen = _braking(monkeypatch)
        gate.handle_dispatch_admission_on_task_create(self._task())
        assert seen == [False]

    def test_an_internal_error_fails_open_without_stderr(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        # The harness ABORTS TaskCreated on any handler stderr, so a crash must be
        # swallowed in-handler rather than left to main()'s traceback-writing swallow.
        monkeypatch.setattr(gate, "_denied_reason", _exploding_probe)
        assert gate.handle_dispatch_admission_on_task_create(self._task()) is False
        assert capsys.readouterr().err == ""


class TestRegistration:
    def test_both_arms_are_registered(self) -> None:
        assert gate.handle_dispatch_admission in router._HANDLERS["PreToolUse"]
        assert gate.handle_dispatch_admission_on_task_create in router._HANDLERS["TaskCreated"]

    def test_the_router_reexports_the_same_objects(self) -> None:
        assert router.handle_dispatch_admission is gate.handle_dispatch_admission
        assert router.handle_dispatch_admission_on_task_create is gate.handle_dispatch_admission_on_task_create


class TestKillSwitchParity:
    def test_the_gate_owns_no_second_flag(self) -> None:
        # #4107 asks for the existing admission_governor_enabled to be the ONE
        # kill-switch; a second flag would let the two drift.
        source = gate.__doc__ or ""
        assert "admission_governor_enabled" in source
        assert "_gate_enabled" not in dir(gate)


class TestDeniedReasonSeam:
    """The one place the hook reaches core — the seam every other test patches out."""

    def test_an_unbootstrappable_django_admits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bootstrap, "bootstrap_teatree_django", lambda: False)
        assert _REAL_DENIED_REASON(apply_ceiling=True) is None

    def test_it_forwards_the_ceiling_flag_to_core(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(core, "governor_enabled", lambda: False)
        seen: list[bool] = []
        monkeypatch.setattr(
            core,
            "dispatch_admission_denied_reason",
            lambda *, apply_ceiling, session_id="": seen.append(apply_ceiling) or None,
        )
        assert _REAL_DENIED_REASON(apply_ceiling=False) is None
        assert seen == [False]


class TestColdImport:
    def test_imports_with_stdlib_only_no_django(self) -> None:
        """The live hook is a bare ``python3`` subprocess with no Django configured."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from hooks.scripts import dispatch_admission_gate as g; "
                    "assert 'django' not in sys.modules, 'django imported at module top'; "
                    "assert not any(m == 'teatree' or m.startswith('teatree.') for m in sys.modules), "
                    "'teatree imported at module top'; "
                    "print(sorted(g.DISPATCH_TOOLS))"
                ),
                str(_PLUGIN_ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "['Agent', 'Task']"
