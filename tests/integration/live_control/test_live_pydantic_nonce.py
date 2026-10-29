"""Opt-in router proof that a live provider sees admitted peer input."""

import asyncio
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx2 as httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents import pydantic_ai_config
from teatree.agents.live_client import LiveClient, LiveOfflineError
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.core.models import Session, Task
from teatree.llm.credentials import CredentialError
from tests.factories import planned_ticket
from tests.integration.live_control.live_pydantic import (
    Evidence,
    Operator,
    closing_provider_clients,
    live_harness,
    live_runtime,
    resolve_live_route,
)

_NONCE = "PINEAPPLE-MAILBOX-NONCE"


class _RequestGate:
    """Holds the first provider request open so a peer message is admitted before it is answered."""

    def __init__(self) -> None:
        self.first_request = threading.Event()
        self.release = threading.Event()
        self.bodies: list[str] = []

    async def observe(self, request: httpx.Request) -> None:
        self.bodies.append(request.content.decode())
        if len(self.bodies) == 1:
            self.first_request.set()
            await asyncio.to_thread(self.release.wait, 20)

    @contextmanager
    def installed(self) -> Generator[None]:
        original_client = pydantic_ai_config.DefaultAsyncHttpxClient

        def observing_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            hooks = dict(kwargs.pop("event_hooks", {}))
            hooks["request"] = [self.observe]
            return original_client(*args, event_hooks=hooks, **kwargs)

        with patch.object(pydantic_ai_config, "DefaultAsyncHttpxClient", observing_client):
            yield


@pytest.mark.integration
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("pass_store")
class LivePydanticNonceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PYDANTIC_MAILBOX") != "1":
            self.skipTest("set T3_LIVE_PYDANTIC_MAILBOX=1 to run the live router mailbox proof")
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
        try:
            self.route = resolve_live_route()
        except CredentialError as exc:
            self.skipTest(str(exc))

    def test_a_real_router_run_carries_the_admitted_nonce_in_its_next_request_body(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        broker = shared_broker()
        sender = broker.register(room=str(ticket.pk), harness="claude_sdk", label="live-sender")
        client = MailboxClient(sender.socket_path, sender.token)
        gate = _RequestGate()
        receipt: dict[str, Any] = {}

        async def run() -> HarnessOutcome:
            async with closing_provider_clients():
                return await run_gated()

        async def run_gated() -> HarnessOutcome:
            with gate.installed():
                return await drive_with_heartbeat(
                    task,
                    "Use Bash to run `sleep 8 && echo done` once, then give a one-sentence summary.",
                    ClaudeAgentOptions(max_turns=6),
                    live_harness(self.route),
                    runtime=live_runtime(),
                )

        def one_pending_input() -> bool:
            try:
                return LiveClient().inspect(task.pk)["pending_inputs"] == 1
            except LiveOfflineError:
                return False

        def send_while_first_request_waits(operator: Operator) -> None:
            try:
                if not operator.wait_for(gate.first_request.is_set, what="first provider request"):
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
                if operator.wait_for(one_pending_input, what="pending live input"):
                    gate.release.set()
                sending.join(timeout=30)
            finally:
                gate.release.set()

        outcome: HarnessOutcome | None = None
        with Evidence("nonce", model=self.route.backend.model) as evidence:
            operator = Operator(send_while_first_request_waits)
            try:
                outcome = asyncio.run(run())
                evidence.outcome = outcome
                operator.finish()
            finally:
                operator.stop()
                gate.release.set()
                evidence.facts.update(
                    receipt=receipt,
                    request_count=len(gate.bodies),
                    nonce_request_indexes=[index for index, body in enumerate(gate.bodies) if _NONCE in body],
                    result_subtype=outcome.result_message.subtype if outcome and outcome.result_message else None,
                )
                broker.unregister(sender.address)
            assert receipt["outcome"] == "accepted_current_turn"
            assert receipt["queued_in_inbox"] is True
            assert len(gate.bodies) >= 2
            assert _NONCE not in gate.bodies[0]
            assert gate.bodies[1].count(_NONCE) == 1
