"""Opt-in router proof that a live provider sees admitted peer input."""

import asyncio
import json
import os
import sys
import threading
from typing import Any

import httpx2 as httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.live_client import LiveClient
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents.test_live_control_integration import _until as wait_until

_REQUIRED = ("OPENAI_API_KEY", "T3_LIVE_PYDANTIC_BASE_URL", "T3_LIVE_PYDANTIC_MODEL")
_NONCE = "PINEAPPLE-MAILBOX-NONCE"


@pytest.mark.integration
@pytest.mark.timeout(300)
class LivePydanticNonceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PYDANTIC_MAILBOX") != "1":
            self.skipTest("set T3_LIVE_PYDANTIC_MAILBOX=1 to run the live router mailbox proof")
        missing = [name for name in _REQUIRED if not os.environ.get(name)]
        if missing:
            self.skipTest(f"missing live router configuration: {', '.join(missing)}")

    def test_a_real_router_run_carries_the_admitted_nonce_in_its_next_request_body(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        broker = shared_broker()
        sender = broker.register(room=str(ticket.pk), harness="claude_sdk", label="live-sender")
        client = MailboxClient(sender.socket_path, sender.token)
        first_request = threading.Event()
        release_request = threading.Event()
        requests: list[str] = []
        receipt: dict[str, Any] = {}

        async def observe(request: httpx.Request) -> None:
            requests.append(request.content.decode())
            if len(requests) == 1:
                first_request.set()
                await asyncio.to_thread(release_request.wait, 20)

        async def run() -> HarnessOutcome:
            async with httpx.AsyncClient(event_hooks={"request": [observe]}) as http_client:
                model = OpenAIChatModel(
                    os.environ["T3_LIVE_PYDANTIC_MODEL"],
                    provider=OpenAIProvider(
                        base_url=os.environ["T3_LIVE_PYDANTIC_BASE_URL"],
                        api_key=os.environ["OPENAI_API_KEY"],
                        http_client=http_client,
                    ),
                )
                runtime = HeartbeatRuntime(
                    watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
                    heartbeat_interval=60,
                    sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
                    renew_lease=lambda _task: None,
                    drain_reason=lambda: "",
                )
                harness = GuardedHarness(
                    harness=PydanticAiHarness(model=model, phase="coding"), guard=CompactionGuard(), name="pydantic_ai"
                )
                return await drive_with_heartbeat(
                    task,
                    "Use Bash to run `sleep 8 && echo done` once, then give a one-sentence summary.",
                    ClaudeAgentOptions(),
                    harness,
                    runtime=runtime,
                )

        def operator() -> None:
            try:
                if not first_request.wait(60):
                    return
                peers = asyncio.run(client.peers())
                recipient = next(peer for peer in peers if peer["ticket"] == str(ticket.pk))
                sending = threading.Thread(
                    target=lambda: receipt.update(
                        asyncio.run(client.send(recipient["address"], _NONCE, "live-pydantic-nonce"))
                    ),
                    daemon=True,
                )
                sending.start()
                wait_until(lambda: LiveClient().inspect(task.pk)["pending_inputs"] == 1, "pending live input")
                release_request.set()
                sending.join(timeout=30)
            finally:
                release_request.set()

        thread = threading.Thread(target=operator, daemon=True)
        thread.start()
        try:
            outcome = asyncio.run(run())
            thread.join(timeout=35)
            evidence = {
                "receipt": receipt,
                "request_count": len(requests),
                "nonce_request_indexes": [index for index, body in enumerate(requests) if _NONCE in body],
                "result_subtype": outcome.result_message.subtype if outcome.result_message else None,
            }
            sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")
            assert receipt["outcome"] == "accepted_current_turn"
            assert receipt["queued_in_inbox"] is True
            assert len(requests) >= 2
            assert _NONCE not in requests[0]
            assert requests[1].count(_NONCE) == 1
        finally:
            release_request.set()
            broker.unregister(sender.address)
