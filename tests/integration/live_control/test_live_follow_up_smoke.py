"""Opt-in real Claude follow-up smoke with both SDK result messages printed."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import claude_agent_sdk
import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
from django.test import TestCase

from teatree.agents.claude_sdk_session import ClaudeSdkSession
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.live_client import LiveClient, LiveOfflineError
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.model_tiering import TIER_MODELS
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Session, Task
from teatree.credential_config import resolve_subscription_credential
from teatree.llm.credentials import CredentialError
from tests.factories import planned_ticket


@dataclass
class _RecordingSession:
    session: ClaudeSdkSession
    results: list[ResultMessage]

    async def query(self, prompt: str) -> None:
        await self.session.query(prompt)

    async def follow_up(self, prompt: str) -> None:
        await self.session.follow_up(prompt)

    async def interrupt(self) -> None:
        await self.session.interrupt()

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        await self.session.steer(text, input_id=input_id, wait=wait)

    async def receive_response(self) -> AsyncIterator[object]:
        async for message in self.session.receive_response():
            if isinstance(message, ResultMessage):
                self.results.append(message)
            yield message


@dataclass
class _RecordingHarness(ClaudeSdkHarness):
    results: list[ResultMessage] = field(default_factory=list)

    @asynccontextmanager
    async def open(self, options: ClaudeAgentOptions) -> AsyncGenerator[_RecordingSession]:
        async with ClaudeSdkHarness.open(options) as session:
            assert isinstance(session, ClaudeSdkSession)
            yield _RecordingSession(session, self.results)


@pytest.mark.integration
@pytest.mark.timeout(360)
class LiveFollowUpSmokeTests(TestCase):
    def test_real_claude_starts_one_follow_up_turn_and_replays_its_receipt(self) -> None:
        if os.environ.get("T3_LIVE_CONTROL_SMOKE") != "1":
            self.skipTest("T3_LIVE_CONTROL_SMOKE is not enabled")
        try:
            child_env = resolve_subscription_credential(scope="").child_env(os.environ)
        except CredentialError as exc:
            self.skipTest(str(exc))
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        shared_broker()
        nonce = uuid.uuid4().hex[:12]
        client = LiveClient()
        seen: dict[str, object] = {}
        options = ClaudeAgentOptions(
            model=TIER_MODELS["cheap"],
            permission_mode="bypassPermissions",
            cwd=tempfile.mkdtemp(prefix="t3-follow-up-smoke-"),
            setting_sources=[],
            max_turns=6,
            env=child_env,
        )
        runtime = HeartbeatRuntime(
            watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
            heartbeat_interval=60,
            sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
            renew_lease=lambda _task: None,
            drain_reason=lambda: "",
            idle_window_seconds=60,
        )

        def operator() -> None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    rows = client.sessions()
                except LiveOfflineError:
                    rows = []
                if any(row["task"] == task.pk and row["state"] == "idle" for row in rows):
                    text = f"Reply with {nonce} and finish with one JSON result envelope."
                    seen["first"] = client.follow_up(task.pk, text, command_id="smoke-follow-up", wait=60)
                    seen["second"] = client.follow_up(task.pk, text, command_id="smoke-follow-up", wait=60)
                    return
                time.sleep(0.2)

        recording = _RecordingHarness()
        harness = GuardedHarness(harness=recording, guard=CompactionGuard(), name="claude_sdk")
        thread = threading.Thread(target=operator)
        thread.start()
        outcome = asyncio.run(
            drive_with_heartbeat(
                task,
                "Reply with one word.",
                options,
                harness,
                runtime=runtime,
            )
        )
        thread.join(timeout=5)
        cli = shutil.which("claude")
        evidence = {
            "first_receipt": seen.get("first"),
            "second_receipt": seen.get("second"),
            "results": [
                {
                    "session_id": row.session_id,
                    "total_cost_usd": row.total_cost_usd,
                    "num_turns": row.num_turns,
                    "usage": row.usage,
                }
                for row in recording.results
            ],
            "claude_agent_sdk": claude_agent_sdk.__version__,
            "claude_cli": (
                subprocess.run([cli, "-v"], capture_output=True, text=True, check=False).stdout.strip()
                if cli
                else "unavailable"
            ),
        }
        sys.stdout.write(json.dumps(evidence, default=str) + "\n")
        assert seen["first"] == seen["second"]
        first = seen["first"]
        assert isinstance(first, dict)
        assert first["outcome"] == "accepted_new_turn"
        assert len(recording.results) == 2
        assert nonce in outcome.agent_text
