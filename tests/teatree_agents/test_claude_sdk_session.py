"""Operator input joins a dispatch's hook lists after the gates the dispatch already arms."""

import asyncio
from typing import Any

from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookJSONOutput, StopHookInput

from teatree.agents.claude_sdk_session import LiveInputQueue
from teatree.agents.envelope_stop_gate import EnvelopeStopGate, envelope_stop_hooks
from teatree.agents.subagent_ceiling import SpawnCeiling, spawn_ceiling_hooks


def _dispatch_hooks() -> dict[Any, list[HookMatcher]]:
    return spawn_ceiling_hooks(SpawnCeiling(limit=3)) | envelope_stop_hooks(EnvelopeStopGate("coding", limit=2))


def _stop_answer_with_input_waiting(queue: LiveInputQueue, stop: HookMatcher) -> HookJSONOutput:
    stop_input: StopHookInput = {
        "session_id": "s1",
        "transcript_path": "",
        "cwd": "",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }

    async def run() -> HookJSONOutput:
        steering = asyncio.create_task(queue.submit("use docs/x.md", wait=5))
        await asyncio.sleep(0)
        answer = await stop.hooks[0](stop_input, None, {"signal": None})
        await steering
        return answer

    return asyncio.run(run())


def test_the_dispatchs_own_gates_stay_armed_and_run_first() -> None:
    hooks = _dispatch_hooks()
    armed = {event: list(matchers) for event, matchers in hooks.items()}

    merged = LiveInputQueue().hooks_merged_into(hooks)

    assert merged["PreToolUse"] == armed["PreToolUse"]
    assert merged["Stop"][:-1] == armed["Stop"]
    assert len(merged["PostToolUse"]) == 1
    assert hooks == armed


def test_the_appended_stop_hook_delivers_the_waiting_input() -> None:
    queue = LiveInputQueue()

    merged = queue.hooks_merged_into(_dispatch_hooks())

    assert _stop_answer_with_input_waiting(queue, merged["Stop"][-1]) == {
        "decision": "block",
        "reason": "use docs/x.md",
    }
