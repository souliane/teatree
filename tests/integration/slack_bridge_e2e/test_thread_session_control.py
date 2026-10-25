"""A Slack thread steers, pauses and resumes a ticket's task, and the pause survives a worker restart (#5280).

Only Slack, the model stream and the clock are fakes: the Slack answer cycle, the worker socket, the
operator ingress, the live registry, the heartbeat driver, the pause park and the resume release run for real.
"""

import asyncio
import os
import tempfile
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase
from django.utils import timezone

from teatree.agents.live_client import LiveClient
from teatree.agents.live_ingress import OperatorIngress
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_registry import reset_shared_registries, shared_registry
from teatree.agents.prompt import build_task_prompt
from teatree.agents.runner import _outcome_failure
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.agents.session_lineage import resume_session_id
from teatree.core.models import DmContext, PendingChatInjection, SessionCommand, Task
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.orchestration import WorkOrigin, dispatch_work
from tests.teatree_agents._sdk_fake import InterruptibleSession, OneSessionHarness, assistant_text
from tests.teatree_loop.slack_answer.test_cycle import _USER_UID, RecordingBackend, _resolver

pytestmark = pytest.mark.integration

_SESSION_ID = "0f1e2d3c-4b5a-4968-8776-655443322110"
_WATCHDOG = LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0)


class _ScriptedSession(InterruptibleSession):
    def __init__(self) -> None:
        super().__init__([assistant_text("working on the change")], session_id=_SESSION_ID)
        self.steered: list[str] = []

    async def steer(self, text: str, *, input_id: str, wait: float) -> None:
        self.steered.append(text)


def _runtime() -> HeartbeatRuntime:
    return HeartbeatRuntime(
        watchdog=_WATCHDOG,
        heartbeat_interval=0.05,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
    )


@contextmanager
def _worker_socket() -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="t3e-", dir="/tmp") as directory:
        live_dir = Path(directory)
        ingress = OperatorIngress(shared_registry, worker_pid=os.getpid())
        with LiveMailboxBroker(runtime_dir=live_dir, ingress=ingress):
            yield live_dir


class ThreadSessionControlTests(TestCase):
    def setUp(self) -> None:
        reset_shared_registries()
        self.addCleanup(reset_shared_registries)
        self.backend = RecordingBackend()
        self.next_ts = 101

    def _say(self, text: str, live_dir: Path | None) -> str:
        ts = f"{self.next_ts}.0"
        self.next_ts += 1
        row = PendingChatInjection.record(
            channel="C1", slack_ts=ts, text=text, context=DmContext(user_id=_USER_UID, thread_ts="100.0")
        )
        assert row is not None
        self.backend.message_meta[ts] = {"ts": ts, "thread_ts": "100.0"}
        client = (lambda: LiveClient(live_dir=live_dir)) if live_dir is not None else LiveClient
        with patch("teatree.loop.slack_answer.session_routing.LiveClient", client):
            run_slack_answer_cycle(messaging_resolver=_resolver(self.backend))
        return self.backend.replies[-1][2]

    def _await_live(self, task: Task) -> None:
        for _ in range(200):
            if shared_registry().facts(task.pk) is not None:
                return
            asyncio.run(asyncio.sleep(0.05))
        pytest.fail("the scripted session never published itself")

    def test_pause_and_resume_from_the_thread_survive_a_worker_restart(self) -> None:
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION, answerable=False, work_summary="build feature", source=ReadingSource.MODEL
        )
        task = dispatch_work(
            reading=reading,
            fingerprint="thread-control",
            origin=WorkOrigin("", "C1", "100.0", ("100.0",), "build feature"),
        )
        task.claim(claimed_by="worker-A", lease_seconds=900)
        session = _ScriptedSession()

        def drive() -> HarnessOutcome:
            options = ClaudeAgentOptions()
            return asyncio.run(drive_with_heartbeat(task, "p", options, OneSessionHarness(session), runtime=_runtime()))

        with _worker_socket() as live_dir, ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(drive)
            self._await_live(task)
            steered = self._say("Use the new API", live_dir)
            paused = self._say("pause", live_dir)
            outcome = running.result(timeout=30)

        assert "accepted_current_turn" in steered
        assert "paused" in paused
        assert [text.splitlines()[1] for text in session.steered] == ["Use the new API"]
        assert outcome.paused is True
        assert outcome.session_id == _SESSION_ID

        attempt = _outcome_failure(task, outcome, phase=task.phase)
        task.refresh_from_db()
        assert attempt is not None
        assert attempt.error.startswith("operator_paused:")
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before > timezone.now() + timedelta(days=3649)

        reset_shared_registries()
        queued = self._say("Also update the docs", None)
        resumed = self._say("resume", None)

        assert "queued for the same task's next run" in queued
        assert "queued follow-up (same task)" in resumed
        task.refresh_from_db()
        assert Task.objects.count() == 1
        assert task.status == Task.Status.PENDING
        assert task.not_before is None
        assert task.session_continuation == Task.SessionContinuation.SELF
        assert resume_session_id(task) == _SESSION_ID
        assert "Also update the docs" in build_task_prompt(task)
        outcomes: dict[str, Any] = dict(SessionCommand.objects.values_list("text", "outcome"))
        assert outcomes == {
            "Use the new API": "accepted_current_turn",
            "pause": "paused",
            "Also update the docs": "queued",
            "resume": "queued_resume",
        }
