"""Opt-in real read-only phase exchange and MCP grant proof."""

import asyncio
import os
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.live_mailbox import shared_broker
from teatree.core.modelkit.phase_tools import mcp_write_tools_for_phase
from teatree.mcp.server import build_server
from tests.integration.live_control.live_proof import run_pair


@pytest.mark.integration
@pytest.mark.timeout(360)
class LivePhaseTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PHASES") != "1":
            self.skipTest("set T3_LIVE_PHASES=1 for authenticated phase proof")

    def _prove_phase(self, phase: str) -> None:
        broker = shared_broker()
        identity = broker.register(room="live-phase-catalog", harness="claude_sdk", label="catalog")
        try:
            with (
                patch.dict(os.environ, identity.mcp_env()),
                patch("teatree.mcp.server.get_all_overlays", return_value={}),
            ):
                server = build_server(read_only=True, allowed_writes=mcp_write_tools_for_phase(phase))
                tools = asyncio.run(server.list_tools())
        finally:
            broker.unregister(identity.address)
        writable = {tool.name for tool in tools if not tool.annotations or not tool.annotations.read_only_hint}
        assert writable == {"agent_mailbox_send", "agent_mailbox_inbox", "agent_mailbox_wait", "escalate"}
        run_pair("claude_sdk", "claude_sdk", phase=phase, sender_phase=phase, round_trip=True)

    def test_planning_sends_and_receives_with_mailbox_only_writes(self) -> None:
        self._prove_phase("planning")

    def test_reviewing_sends_and_receives_with_mailbox_only_writes(self) -> None:
        self._prove_phase("reviewing")

    def test_critic_reviewing_sends_and_receives_with_mailbox_only_writes(self) -> None:
        self._prove_phase("critic_reviewing")
