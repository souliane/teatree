"""Opt-in live Pydantic sender, pause, follow-up and compaction proofs."""

import asyncio
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from typing import Any
from unittest import SkipTest
from unittest.mock import Mock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart

from teatree.agents.harness import PydanticAiHarness
from teatree.agents.live_client import LiveClient, LiveOfflineError
from teatree.agents.live_control import frame_operator_input
from teatree.agents.live_delivery import InboxMessage
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.core.models import Session, Task, TaskAttempt
from teatree.core.models.task_handoff import schedule_resume
from teatree.llm.credentials import CredentialError
from tests.factories import planned_ticket
from tests.integration.live_control.live_pydantic import LIVE_ROUTE_MODEL, live_runtime, resolve_live_route


def _evidence(name: str, outcome: HarnessOutcome, **facts: object) -> None:
    result = outcome.result_message
    sys.stdout.write(
        json.dumps(
            {
                "node": name,
                "request_count": result.num_turns if result is not None else None,
                "cost_usd": result.total_cost_usd if result is not None else None,
                "paused": outcome.paused,
                "tool_calls": outcome.tool_calls,
                **facts,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _contains_user_prompt(thread: list[ModelRequest | ModelResponse], text: str) -> bool:
    return any(
        text in part.content
        for message in thread
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def test_compaction_steer_is_found_in_prompt_content() -> None:
    steer = frame_operator_input("Preserve the public API after compaction", "pydantic-compaction")
    thread = [ModelRequest(parts=[UserPromptPart(content=steer)])]
    assert steer not in repr(thread)
    assert _contains_user_prompt(thread, steer)


def test_pydantic_live_flag_stops_before_route_resolution() -> None:
    case = LivePydanticLaneTests(methodName="test_sender_calls_mailbox_tool_and_recipient_reads_the_nonce")
    route = Mock(side_effect=AssertionError("route resolved without the live flag"))
    with (
        patch.dict(os.environ, {"T3_LIVE_PYDANTIC_MAILBOX": "0"}),
        patch.dict(LivePydanticLaneTests.setUp.__globals__, {"resolve_live_route": route}),
    ):
        try:
            with pytest.raises(SkipTest, match="T3_LIVE_PYDANTIC_MAILBOX=1"):
                case.setUp()
        finally:
            case.doCleanups()


def test_pydantic_live_setup_removes_generic_provider_key() -> None:
    def reject_generic_key() -> None:
        assert "OPENAI_COMPATIBLE_API_KEY" not in os.environ
        message = "route unavailable"
        raise CredentialError(message)

    case = LivePydanticLaneTests(methodName="test_sender_calls_mailbox_tool_and_recipient_reads_the_nonce")
    with (
        patch.dict(os.environ, {"T3_LIVE_PYDANTIC_MAILBOX": "1", "OPENAI_COMPATIBLE_API_KEY": "sentinel"}),
        patch.dict(LivePydanticLaneTests.setUp.__globals__, {"resolve_live_route": reject_generic_key}),
    ):
        try:
            with pytest.raises(SkipTest, match="route unavailable"):
                case.setUp()
        finally:
            case.doCleanups()


@pytest.mark.integration
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("pass_store")
class LivePydanticLaneTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PYDANTIC_MAILBOX") != "1":
            self.skipTest("set T3_LIVE_PYDANTIC_MAILBOX=1 for the Pydantic live proofs")
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
        try:
            self.route = resolve_live_route()
        except CredentialError as exc:
            self.skipTest(str(exc))
        self.ticket = planned_ticket()
        self.task = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="coding"
        )
        shared_broker()

    async def _drive(
        self,
        task: Task,
        prompt: str,
        *,
        history: list[ModelRequest | ModelResponse] | None = None,
        context_windows: dict[str, int] | None = None,
        idle_window_seconds: float = 0,
    ) -> HarnessOutcome:
        config = resolve_live_route(context_windows=context_windows) if context_windows else self.route
        harness = PydanticAiHarness(history=history, phase="coding", config=config)
        with tempfile.TemporaryDirectory(prefix="t3-pydantic-live-") as cwd:
            return await drive_with_heartbeat(
                task,
                prompt,
                ClaudeAgentOptions(cwd=cwd, max_turns=6),
                harness,
                runtime=live_runtime(idle_window_seconds=idle_window_seconds),
            )

    def test_sender_calls_mailbox_tool_and_recipient_reads_the_nonce(self) -> None:
        broker = shared_broker()
        recipient = broker.register(room=str(self.ticket.pk), harness="claude_sdk", label="live-recipient")
        peer = MailboxClient(recipient.socket_path, recipient.token)
        nonce = secrets.token_hex(12)

        async def exchange() -> tuple[HarnessOutcome, InboxMessage]:
            async def receive() -> InboxMessage:
                for _ in range(6):
                    page = await peer.wait(timeout_seconds=20)
                    if page["messages"]:
                        return page["messages"][0]
                pytest.fail("Pydantic did not send the nonce")

            outcome, message = await asyncio.gather(
                self._drive(
                    self.task,
                    "Use agent_mailbox_peers, then agent_mailbox_send to send exactly "
                    f"{nonce} to live-recipient at {recipient.address} with a fresh key. Then finish.",
                ),
                receive(),
            )
            return outcome, message

        try:
            outcome, message = asyncio.run(exchange())
        finally:
            broker.unregister(recipient.address)
        _evidence("sender", outcome, inbox_nonce=message["text"] == nonce)
        assert message["text"] == nonce
        assert outcome.tool_calls >= 2

    def test_pause_preserves_completed_tool_work_and_resume_reads_it(self) -> None:
        nonce = secrets.token_hex(12)
        seen: dict[str, Any] = {}
        client = LiveClient()

        def operator() -> None:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                try:
                    facts = client.inspect(self.task.pk)
                except LiveOfflineError:
                    facts = None
                if facts is not None and facts["open_tool"] == "Bash" and facts["tool_calls"] >= 2:
                    seen["receipt"] = client.pause(self.task.pk, "pause", command_id="pydantic-pause", wait=20)
                    return
                time.sleep(0.1)
            seen["error"] = "second Bash tool never opened"

        thread = threading.Thread(target=operator, daemon=True)
        thread.start()
        outcome = asyncio.run(
            self._drive(
                self.task,
                f"Use Bash to run `echo {nonce}` first. After it finishes, use Bash to run `sleep 60`. "
                "Do not finish before the second command completes.",
            )
        )
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert "error" not in seen, seen
        assert seen["receipt"]["outcome"] == "paused"
        assert outcome.paused
        assert outcome.thread is not None
        assert any(
            nonce in str(part.content)
            for message in outcome.thread
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        )
        persist_parked_thread(self.task, outcome.thread)
        TaskAttempt.objects.create(task=self.task, result={"needs_user_input": True, "summary": "paused"})
        resumed_task = schedule_resume(self.task, answer="Continue after pause")
        resumed = rehydrate_thread_for_resume(resumed_task)
        assert resumed is not None
        resumed_outcome = asyncio.run(
            self._drive(
                resumed_task,
                "Without using tools, report the exact output of the first Bash command before the pause.",
                history=resumed.history,
            )
        )
        _evidence(
            "pause_resume",
            outcome,
            resumed_request_count=resumed_outcome.result_message.num_turns if resumed_outcome.result_message else None,
        )
        assert nonce in resumed_outcome.agent_text
        assert resumed_outcome.tool_calls == 0

    def test_idle_follow_up_replays_one_receipt_and_starts_one_turn(self) -> None:
        nonce = secrets.token_hex(12)
        seen: dict[str, Any] = {}
        client = LiveClient()

        def operator() -> None:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                try:
                    rows = client.sessions()
                except LiveOfflineError:
                    rows = []
                if any(row["task"] == self.task.pk and row["state"] == "idle" for row in rows):
                    prompt = f"Reply with {nonce} in one sentence."
                    seen["first"] = client.follow_up(self.task.pk, prompt, command_id="pydantic-follow-up", wait=30)
                    seen["replay"] = client.follow_up(self.task.pk, prompt, command_id="pydantic-follow-up", wait=30)
                    return
                time.sleep(0.1)
            seen["error"] = "idle window never opened"

        thread = threading.Thread(target=operator, daemon=True)
        thread.start()
        outcome = asyncio.run(self._drive(self.task, "Reply with ready.", idle_window_seconds=8))
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert "error" not in seen, seen
        assert seen["first"] == seen["replay"]
        assert seen["first"]["outcome"] == "accepted_new_turn"
        assert nonce in outcome.agent_text
        assert outcome.thread is not None
        user_prompts = [
            part.content
            for message in outcome.thread
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        _evidence("idle_follow_up", outcome, follow_up_prompts=sum(nonce in str(part) for part in user_prompts))
        assert sum(nonce in str(part) for part in user_prompts) == 1

    def test_compaction_fires_only_at_the_small_window_and_pins_the_operator_steer(self) -> None:
        steer = frame_operator_input("Preserve the public API after compaction", "pydantic-compaction")
        history = [
            ModelRequest(parts=[UserPromptPart(content="Original task")]),
            ModelResponse(parts=[TextPart(content="first " * 3000)]),
            ModelRequest(parts=[UserPromptPart(content=steer)]),
            ModelResponse(parts=[TextPart(content="second " * 3000)]),
            ModelRequest(parts=[UserPromptPart(content="Continue")]),
        ]
        outcomes: dict[str, HarnessOutcome] = {}
        for label, window in (("small", 20_000), ("large", 1_000_000)):
            task = Task.objects.create(
                ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="coding"
            )
            outcomes[label] = asyncio.run(
                self._drive(
                    task,
                    "Reply with one sentence without tools.",
                    history=history,
                    context_windows={LIVE_ROUTE_MODEL: window},
                )
            )
        small, large = outcomes["small"], outcomes["large"]
        assert small.thread is not None
        assert large.thread is not None
        small_history, large_history = repr(small.thread), repr(large.thread)
        _evidence("compaction", small, summary_present="Earlier conversation summary:" in small_history)
        assert "Earlier conversation summary:" in small_history
        assert "Earlier conversation summary:" not in large_history
        assert _contains_user_prompt(small.thread, steer)
