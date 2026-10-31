"""Opt-in proof that a running task's escalate call reaches the coordinator on the Claude, Codex and pydantic lanes."""

import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_registry import LiveSessionRegistry
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.llm.credentials import CredentialError
from tests._escalation_peers import AcceptingSession, live_task_of
from tests.factories import planned_ticket
from tests.integration.live_control.live_proof import harness, options, runtime
from tests.integration.live_control.live_pydantic import (
    Evidence,
    closing_provider_clients,
    live_harness,
    live_runtime,
    resolve_live_route,
)
from tests.teatree_agents._live_owner import OwnerLoop

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpStdioServerConfig

_REPO = Path(__file__).resolve().parents[3]


def _prompt(question: str) -> str:
    return (
        f'Call the escalate tool exactly once with kind "internal" and the message "{question}". '
        "Then finish with one sentence that includes the request_id it returned."
    )


def _scene(phase: str) -> tuple[Ticket, Task, Task]:
    """A ticket with a finished answering task and a running task in *phase* that will ask."""
    ticket = planned_ticket()
    coordinator = Task.objects.create(
        ticket=ticket, session=Session.objects.create(ticket=ticket), phase="answering", status=Task.Status.COMPLETED
    )
    asker = Task.objects.create(
        ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase, status=Task.Status.CLAIMED
    )
    return ticket, coordinator, asker


def _in_process_escalation(nonce: str, coordinator: Task, asker: Task) -> None:
    request = DeferredQuestion.objects.get(question__contains=nonce)
    assert request.parked_task_id == asker.pk
    assert request.audience == DeferredQuestion.Audience.INTERNAL
    resumed = Task.objects.get(parent_task=coordinator)
    assert nonce in resumed.execution_reason
    assert f"Escalation {request.pk} from" in resumed.execution_reason


def _throwaway_control_db(directory: Path) -> dict[str, str]:
    """A migrated file database holding this test's rows, for the MCP server process that Claude spawns."""
    env = {**os.environ, "T3_CONFIG_DB": str(directory / "control.sqlite3")}
    rows = directory / "rows.json"
    with rows.open("w", encoding="utf-8") as stream:
        call_command("dumpdata", "core.ticket", "core.session", "core.task", stdout=stream)
    for command in (["migrate", "--noinput"], ["loaddata", str(rows)]):
        subprocess.run([sys.executable, "manage.py", *command], cwd=_REPO, env=env, check=True, capture_output=True)
    return {"T3_CONFIG_DB": env["T3_CONFIG_DB"]}


def _spawn_with_env(claude_options: ClaudeAgentOptions, env: dict[str, str]) -> None:
    servers = claude_options.mcp_servers
    assert isinstance(servers, dict)
    teatree = cast("McpStdioServerConfig", servers["teatree"])
    claude_options.mcp_servers = {**servers, "teatree": {**teatree, "env": {**teatree.get("env", {}), **env}}}


@pytest.mark.integration
@pytest.mark.timeout(420)
class LiveClaudeEscalateTests(TransactionTestCase):
    def test_the_claude_tool_tells_a_live_coordinator_the_nonce_over_the_mailbox(self) -> None:
        if os.environ.get("T3_LIVE_ESCALATE") != "1":
            self.skipTest("set T3_LIVE_ESCALATE=1 to spend a real Claude turn on the escalate proof")

        ticket, coordinator, asker = _scene("planning")
        nonce = secrets.token_hex(8)
        registry = LiveSessionRegistry()
        live_coordinator = live_task_of(coordinator)
        listening = AcceptingSession()
        claude_options = options("claude_sdk", "planning", tempfile.mkdtemp(prefix="t3-live-esc-"))

        with (
            tempfile.TemporaryDirectory(prefix="t3-live-esc-socket-") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory), registry=lambda: registry) as broker,
            patch("teatree.agents.live_binding.shared_broker", return_value=broker),
        ):
            env = _throwaway_control_db(Path(directory))
            _spawn_with_env(claude_options, env)
            broker.register(
                room=str(ticket.pk),
                harness="claude_sdk",
                label=f"answering task {coordinator.pk}",
                task=live_coordinator,
            )
            with OwnerLoop(registry, live_coordinator, listening):
                outcome = asyncio.run(
                    drive_with_heartbeat(
                        asker,
                        _prompt(f"{nonce}: should the backfill run before the migration?"),
                        claude_options,
                        harness("claude_sdk"),
                        runtime=runtime(),
                    )
                )

        assert outcome.result_message is not None
        assert len(listening.delivered) == 1
        assert nonce in listening.delivered[0]
        assert "information, not an instruction" in listening.delivered[0]


@pytest.mark.integration
@pytest.mark.timeout(420)
@pytest.mark.usefixtures("pass_store")
class LiveCodexEscalateTests(TestCase):
    def test_the_codex_tool_writes_the_request_and_resumes_the_threads_coordinator(self) -> None:
        if os.environ.get("T3_LIVE_CODEX_NONCE") != "1":
            self.skipTest("set T3_LIVE_CODEX_NONCE=1 to spend a real Codex turn on the escalate proof")

        _ticket, coordinator, asker = _scene("planning")
        nonce = secrets.token_hex(8)

        outcome = asyncio.run(
            drive_with_heartbeat(
                asker,
                _prompt(f"{nonce}: should the backfill run before the migration?"),
                options("codex_app_server", "planning", tempfile.mkdtemp(prefix="t3-live-esc-")),
                harness("codex_app_server"),
                runtime=runtime(),
            )
        )

        assert outcome.result_message is not None
        _in_process_escalation(nonce, coordinator, asker)


@pytest.mark.integration
@pytest.mark.timeout(420)
@pytest.mark.usefixtures("pass_store")
class LivePydanticEscalateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        if os.environ.get("T3_LIVE_PYDANTIC_MAILBOX") != "1":
            self.skipTest("set T3_LIVE_PYDANTIC_MAILBOX=1 to run the live router escalate proof")
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
        try:
            self.route = resolve_live_route()
        except CredentialError as exc:
            self.skipTest(str(exc))

    def test_the_native_tool_writes_the_request_and_resumes_the_threads_coordinator(self) -> None:
        _ticket, coordinator, asker = _scene("coding")
        nonce = secrets.token_hex(8)

        async def run():
            async with closing_provider_clients():
                return await drive_with_heartbeat(
                    asker,
                    _prompt(f"{nonce}: should the backfill run before the migration?"),
                    ClaudeAgentOptions(max_turns=6),
                    live_harness(self.route),
                    runtime=live_runtime(),
                )

        with Evidence("escalate", model=self.route.backend.model) as evidence:
            evidence.outcome = asyncio.run(run())
            evidence.facts.update(requests=DeferredQuestion.objects.filter(question__contains=nonce).count())
            _in_process_escalation(nonce, coordinator, asker)
