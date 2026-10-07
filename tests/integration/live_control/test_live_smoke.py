"""Live smoke: steer a real Claude session, driven by the real driver, over the worker's ingress socket.

Opt-in (``T3_LIVE_CONTROL_SMOKE=1``) and authenticated by the ambient subscription token
(``CLAUDE_CODE_OAUTH_TOKEN``). It spends one cheap-tier turn and prints the receipt, which
is quoted as evidence; whether the model then followed the input is printed, not asserted.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any

import claude_agent_sdk
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.live_client import LiveClient
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.model_tiering import TIER_MODELS
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Session, Task
from tests.factories import planned_ticket

_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
_PROMPT = "Run the Bash command `sleep 8 && echo done` exactly once. When it finishes, reply with one short sentence."
_STEER = "Also include the word PINEAPPLE in your final sentence."


def _skip_reason() -> str:
    if os.environ.get("T3_LIVE_CONTROL_SMOKE") != "1":
        return "set T3_LIVE_CONTROL_SMOKE=1 to run the live-control smoke against a real Claude session"
    if not os.environ.get(_TOKEN_ENV):
        return f"{_TOKEN_ENV} is not set: no subscription credential for a real Claude session"
    return ""


_SKIP_REASON = _skip_reason()


@pytest.mark.integration
@pytest.mark.timeout(300)
@pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON)
class LiveSmokeTests(TestCase):
    def test_a_real_running_session_accepts_a_steer_into_its_current_turn(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        shared_broker()
        client = LiveClient()
        options = ClaudeAgentOptions(
            model=TIER_MODELS["cheap"],
            permission_mode="bypassPermissions",
            cwd=tempfile.mkdtemp(prefix="t3-live-smoke-"),
            setting_sources=[],
            max_turns=8,
            env={_TOKEN_ENV: os.environ[_TOKEN_ENV]},
        )
        runtime = HeartbeatRuntime(
            watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
            heartbeat_interval=60,
            sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
            renew_lease=lambda _task: None,
            drain_reason=lambda: "",
        )
        seen: dict[str, Any] = {}

        def operator() -> None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                rows = client.sessions()
                if rows and rows[0]["open_tool"] == "Bash":
                    seen["inspect"] = client.inspect(task.pk)
                    seen["receipt"] = client.steer(task.pk, _STEER, command_id="smoke-1", wait=60)
                    return
                time.sleep(0.2)

        thread = threading.Thread(target=operator, daemon=True)
        thread.start()
        harness = GuardedHarness(harness=ClaudeSdkHarness(), guard=CompactionGuard(), name="claude_sdk")
        outcome = asyncio.run(drive_with_heartbeat(task, _PROMPT, options, harness, runtime=runtime))
        thread.join(timeout=30)

        evidence = {
            "inspect": seen.get("inspect"),
            "receipt": seen.get("receipt"),
            "result_subtype": outcome.result_message.subtype if outcome.result_message else None,
            "model_followed_input": "PINEAPPLE" in outcome.agent_text,
            "claude_agent_sdk": claude_agent_sdk.__version__,
        }
        sys.stdout.write(json.dumps(evidence, indent=2, default=str) + "\n")
        assert seen["receipt"]["outcome"] == "accepted_current_turn"
        assert outcome.result_message is not None
        assert outcome.stuck_reason is None
