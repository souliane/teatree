# test-path: cross-cutting — tests hooks/scripts/cron_loop_shell_gate.py (hooks/); no src/teatree/ mirror.
"""PreToolUse gate: a cron/wakeup may not shell a ``t3 loop`` command the worker owns (#2663).

The decision core is pinned separately; this covers the hook's own contract —
which tools it reads, the worker-liveness probe it injects, and the three
never-lockout escapes (per-call token, kill-switch, fail-open on a cold env).
"""

import json

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts import cron_loop_shell_gate
from hooks.scripts.hook_router import handle_block_cron_loop_shell

_PROBE_FAILURE = RuntimeError("probe unavailable")

_REAL_WORKER_PROBE = cron_loop_shell_gate._worker_is_alive

_TICK_PROMPT = "Run `t3 loops tick --loop dispatch` in Bash, then briefly report the tick summary."
_REACTIVE_PROMPT = "/loop 30s Run `t3 loop drain-queue run`."


@pytest.fixture(autouse=True)
def _worker_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron_loop_shell_gate, "_worker_is_alive", lambda: True)
    monkeypatch.setattr(cron_loop_shell_gate, "_cron_loop_shell_gate_enabled", lambda: True)


def _cron_event(prompt: str) -> dict:
    return {
        "session_id": "sess-cron-loop",
        "tool_name": "CronCreate",
        "tool_input": {"cron": "*/30 * * * *", "prompt": prompt},
    }


def _wakeup_event(prompt: str) -> dict:
    return {
        "session_id": "sess-cron-loop",
        "tool_name": "ScheduleWakeup",
        "tool_input": {"delaySeconds": 1800, "prompt": prompt, "reason": "drive the loop"},
    }


def _deny_payload(capsys: pytest.CaptureFixture[str]) -> dict | None:
    output = capsys.readouterr().out.strip()
    return json.loads(output) if output else None


class TestDenies:
    @pytest.mark.parametrize("build", [_cron_event, _wakeup_event])
    def test_tick_prompt_is_denied_on_both_surfaces(self, build, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_cron_loop_shell(build(_TICK_PROMPT)) is True

        payload = _deny_payload(capsys)
        assert payload is not None
        assert "t3 loops tick --loop dispatch" in json.dumps(payload)

    def test_reactive_prompt_is_denied_while_a_worker_is_alive(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_cron_loop_shell(_cron_event(_REACTIVE_PROMPT)) is True

        assert _deny_payload(capsys) is not None


class TestAllows:
    def test_unrelated_prompt_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert handle_block_cron_loop_shell(_cron_event("/followup")) is False

        assert _deny_payload(capsys) is None

    def test_reactive_prompt_passes_when_no_worker_is_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cron_loop_shell_gate, "_worker_is_alive", lambda: False)

        assert handle_block_cron_loop_shell(_cron_event(_REACTIVE_PROMPT)) is False

    def test_other_tools_are_out_of_scope(self) -> None:
        event = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "t3 loops tick --loop dispatch"}}

        assert handle_block_cron_loop_shell(event) is False

    def test_malformed_tool_input_passes(self) -> None:
        assert handle_block_cron_loop_shell({"session_id": "s", "tool_name": "CronCreate", "tool_input": []}) is False


class TestNeverLockout:
    def test_kill_switch_disables_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cron_loop_shell_gate, "_cron_loop_shell_gate_enabled", lambda: False)

        assert handle_block_cron_loop_shell(_cron_event(_TICK_PROMPT)) is False

    def test_per_call_token_allows_and_notes(self, capsys: pytest.CaptureFixture[str]) -> None:
        prompt = f"[cron-loop-ok: pre-flip box] {_TICK_PROMPT}"

        assert handle_block_cron_loop_shell(_cron_event(prompt)) is False

        assert "cron-loop-ok" in capsys.readouterr().err

    def test_token_in_the_wakeup_reason_also_allows(self) -> None:
        event = _wakeup_event(_TICK_PROMPT)
        event["tool_input"]["reason"] = "[cron-loop-ok: worker is down]"

        assert handle_block_cron_loop_shell(event) is False

    def test_cold_env_without_the_core_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cron_loop_shell_gate, "_load_core", lambda: None)

        assert handle_block_cron_loop_shell(_cron_event(_TICK_PROMPT)) is False

    def test_deny_routes_through_the_fail_open_chokepoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []
        monkeypatch.setattr(router, "_danger_gate_fail_open_enabled", lambda: True)
        monkeypatch.setattr(router, "emit_pretooluse_deny", lambda reason, **_: seen.append(reason) or True)

        assert handle_block_cron_loop_shell(_cron_event(_TICK_PROMPT)) is False
        assert seen == []


class TestWorkerProbe:
    def test_probe_failure_reports_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A probe that cannot resolve the singleton must not manufacture a live
        # worker — the reactive shape then stays allowed rather than wedging a box.
        def _boom() -> bool:
            raise _PROBE_FAILURE

        monkeypatch.setattr(cron_loop_shell_gate, "_flock_held", _boom)

        assert _REAL_WORKER_PROBE() is False
