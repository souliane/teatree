"""Live control of a running claude_sdk task, end to end through the real runner.

``run_agent`` → ``ClaudeSdkHarness`` → the real SDK → ``fake_claude_cli.py`` (a stream-json
process replaying the recorded 2.1.284 contract), reached by a ``LiveClient`` over the
worker's published ingress socket. The operator thread is in the test process, which is the
trusted worker pid; only the model is faked.
"""

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.live_client import LiveClient, WorkerSessionFacts
from teatree.agents.live_control import MAX_PENDING_INPUTS, ReceiptPayload
from teatree.agents.live_mailbox import shared_broker
from teatree.agents.live_registry import shared_registry
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task
from tests.factories import planned_ticket

_FAKE_CLI = Path(__file__).with_name("fake_claude_cli.py")
_ENVELOPE = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
_WAIT_LIMIT = 30.0


def _until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_LIMIT
    while not predicate():
        if time.monotonic() > deadline:
            msg = f"timed out waiting for {what}"
            raise AssertionError(msg)
        time.sleep(0.02)


def _in_background[T](work: Callable[[], T]) -> Future[T]:
    pool = ThreadPoolExecutor(max_workers=1)
    outcome = pool.submit(work)
    pool.shutdown(wait=False)
    return outcome


class _FakeCliRun:
    """One scripted fake-CLI turn: its script, its gate files and the protocol log it writes."""

    def __init__(self, root: Path, **script: Any) -> None:
        self.root = root
        self.log = root / "cli.jsonl"
        self.release_tool = root / "release-tool"
        self.release_stop = root / "release-stop"
        self.script = {"final_text": json.dumps(_ENVELOPE), **script}

    def events(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def saw(self, event: str) -> bool:
        return any(row["event"] == event for row in self.events())

    def hook_answers(self, hook_event: str | None = None) -> list[dict[str, Any]]:
        return [
            row["answer"]
            for row in self.events()
            if row["event"] == "hook_answer" and hook_event in {None, row["hook_event"]}
        ]

    def deliveries_of(self, command_id: str) -> int:
        marker = f"id={command_id}]"
        return sum(json.dumps(answer).count(marker) for answer in self.hook_answers())

    def stdin_count(self) -> int:
        return sum(1 for row in self.events() if row["event"] == "stdin")

    def user_turns(self) -> int:
        return sum(1 for row in self.events() if row["event"] == "stdin" and row["message"].get("type") == "user")


@contextlib.contextmanager
def _real_sdk_on_fake_cli(run: _FakeCliRun) -> Iterator[None]:
    wrapper = run.root / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_CLI}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)

    def client(*, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        return ClaudeSDKClient(options=replace(options, cli_path=str(wrapper)))

    env = {"T3_FAKE_CLAUDE_SCRIPT": json.dumps(run.script), "T3_FAKE_CLAUDE_LOG": str(run.log)}
    with (
        patch.dict(os.environ, env),
        patch.object(harness_mod, "ClaudeSDKClient", client),
        patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(turns=0, cost_usd=0.0))),
    ):
        yield


class LiveControlIntegrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="t3-live-it-", dir="/tmp"))
        self.ticket = planned_ticket()
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-live")
        self.task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")
        shared_broker()
        self.live = LiveClient()

    def dispatch(self, run: _FakeCliRun, operator: Callable[[], Any]) -> Any:
        with _real_sdk_on_fake_cli(run):
            background = _in_background(operator)
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})
        value = background.result(timeout=_WAIT_LIMIT)
        self.attempt = attempt
        return value

    def steer_async(self, text: str, *, command_id: str, wait: float = 20) -> Future[ReceiptPayload]:
        return _in_background(lambda: self.live.steer(self.task.pk, text, command_id=command_id, wait=wait))

    def pending(self) -> int:
        return int(self.live.inspect(self.task.pk)["pending_inputs"])

    def test_inspect_is_passive_and_reports_the_open_tool(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}])

        def operator() -> tuple[WorkerSessionFacts, list[WorkerSessionFacts], int, int]:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            before = run.stdin_count()
            facts = self.live.inspect(self.task.pk)
            rows = self.live.sessions()
            after = run.stdin_count()
            run.release_tool.touch()
            return facts, rows, before, after

        facts, rows, before, after = self.dispatch(run, operator)

        assert after == before, "a passive inspect wrote to the agent's CLI"
        assert facts["state"] == "busy"
        assert facts["steerable"] is True
        assert facts["harness"] == "claude_sdk"
        assert facts["phase"] == "coding"
        assert facts["open_tool"] == "Bash"
        assert facts["tool_calls"] == 1
        assert [row["task"] for row in rows] == [self.task.pk]
        assert run.hook_answers("PostToolUse") == [{}]
        assert not any(run.hook_answers("Stop"))

    def test_a_busy_steer_is_accepted_into_the_current_turn_exactly_once(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}])
        command_id = f"cmd-{uuid.uuid4().hex[:8]}"

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            steering = self.steer_async("Use the spec at docs/x.md", command_id=command_id)
            _until(lambda: self.pending() == 1, "the steer to be pending")
            run.release_tool.touch()
            return steering.result(timeout=_WAIT_LIMIT)

        receipt = self.dispatch(run, operator)

        assert receipt["outcome"] == "accepted_current_turn"
        assert receipt["command_id"] == command_id
        assert receipt["mode"] == "active"
        assert run.deliveries_of(command_id) == 1
        assert "Use the spec at docs/x.md" in json.dumps(run.hook_answers("PostToolUse"))
        assert run.user_turns() == 1
        assert self.attempt.exit_code == 0
        assert self.attempt.result["summary"] == "Done"

    def test_a_steer_pending_at_the_last_tool_is_delivered_at_the_stop_boundary(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"text": "thinking"}], hold_before_stop=str(self.root / "release-stop"))
        command_id = "stop-boundary"

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_before_stop"), "the turn to reach its Stop boundary")
            steering = self.steer_async("Mention the release note", command_id=command_id)
            _until(lambda: self.pending() == 1, "the steer to be pending")
            run.release_stop.touch()
            return steering.result(timeout=_WAIT_LIMIT)

        receipt = self.dispatch(run, operator)

        assert receipt["outcome"] == "accepted_current_turn"
        blocks = [answer for answer in run.hook_answers() if answer.get("decision") == "block"]
        assert len(blocks) == 1
        assert "Mention the release note" in blocks[0]["reason"]
        assert run.deliveries_of(command_id) == 1

    def test_a_steer_after_the_final_stop_boundary_is_rejected_turn_ended(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash"}], hold_after_stop=str(self.root / "release-stop"))

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_after_stop"), "the turn to pass its final Stop")
            steering = self.steer_async("too late", command_id="late-1")
            _until(lambda: self.pending() == 1, "the late steer to be pending")
            run.release_stop.touch()
            return steering.result(timeout=_WAIT_LIMIT)

        receipt = self.dispatch(run, operator)

        assert receipt["outcome"] == "rejected"
        assert receipt["code"] == "turn_ended"
        assert run.deliveries_of("late-1") == 0
        assert run.user_turns() == 1

    def test_a_steer_not_consumed_within_its_wait_is_withdrawn_and_never_delivered(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}])

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            receipt = self.live.steer(self.task.pk, "only if quick", command_id="slow-1", wait=0.3)
            run.release_tool.touch()
            return receipt

        receipt = self.dispatch(run, operator)

        assert receipt["outcome"] == "rejected"
        assert receipt["code"] == "not_accepted_in_time"
        assert run.deliveries_of("slow-1") == 0

    def test_a_retried_command_id_is_idempotent_and_a_changed_text_is_refused(self) -> None:
        run = _FakeCliRun(
            self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}, {"tool": "Bash"}]
        )

        def operator() -> tuple[ReceiptPayload, ReceiptPayload, ReceiptPayload]:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            steering = self.steer_async("same words", command_id="dup-1")
            _until(lambda: self.pending() == 1, "the steer to be pending")
            run.release_tool.touch()
            first = steering.result(timeout=_WAIT_LIMIT)
            again = self.live.steer(self.task.pk, "same words", command_id="dup-1", wait=5)
            changed = self.live.steer(self.task.pk, "other words", command_id="dup-1", wait=5)
            return first, again, changed

        first, again, changed = self.dispatch(run, operator)

        assert first["outcome"] == "accepted_current_turn"
        assert again == first
        assert changed["outcome"] == "rejected"
        assert changed["code"] == "duplicate_mismatch"
        assert run.deliveries_of("dup-1") == 1

    def test_backpressure_and_size_are_refused_before_anything_is_queued(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}])

        def operator() -> tuple[list[ReceiptPayload], ReceiptPayload, ReceiptPayload]:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            queued = [self.steer_async(f"input {n}", command_id=f"bp-{n}") for n in range(MAX_PENDING_INPUTS)]
            _until(lambda: self.pending() == MAX_PENDING_INPUTS, "the queue to fill")
            overflow = self.live.steer(self.task.pk, "one too many", command_id="bp-over", wait=5)
            oversize = self.live.steer(self.task.pk, "x" * 16_385, command_id="bp-big", wait=5)
            run.release_tool.touch()
            return [steering.result(timeout=_WAIT_LIMIT) for steering in queued], overflow, oversize

        accepted, overflow, oversize = self.dispatch(run, operator)

        assert {receipt["outcome"] for receipt in accepted} == {"accepted_current_turn"}
        assert (overflow["outcome"], overflow["code"]) == ("rejected", "backpressure")
        assert (oversize["outcome"], oversize["code"]) == ("rejected", "too_large")
        assert run.deliveries_of("bp-over") == 0
        assert run.deliveries_of("bp-big") == 0

    def test_a_subagent_tool_boundary_never_consumes_operator_input(self) -> None:
        run = _FakeCliRun(
            self.root,
            steps=[{"tool": "Agent", "hold_until": str(self.root / "release-tool"), "subagent": True}],
        )

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_tool"), "the sub-agent tool to run")
            steering = self.steer_async("for the main thread", command_id="main-only")
            _until(lambda: self.pending() == 1, "the steer to be pending")
            run.release_tool.touch()
            return steering.result(timeout=_WAIT_LIMIT)

        receipt = self.dispatch(run, operator)

        assert receipt["outcome"] == "accepted_current_turn"
        assert run.hook_answers("PostToolUse") == [{}]
        assert [answer.get("decision") for answer in run.hook_answers("Stop") if answer] == ["block"]
        assert run.deliveries_of("main-only") == 1

    def test_a_finished_session_answers_closed_and_still_replays_its_receipts(self) -> None:
        run = _FakeCliRun(self.root, steps=[{"tool": "Bash", "hold_until": str(self.root / "release-tool")}])

        def operator() -> ReceiptPayload:
            _until(lambda: run.saw("holding_tool"), "the agent to sit in its tool")
            steering = self.steer_async("last words", command_id="kept-1")
            _until(lambda: self.pending() == 1, "the steer to be pending")
            run.release_tool.touch()
            return steering.result(timeout=_WAIT_LIMIT)

        accepted = self.dispatch(run, operator)

        assert self.live.sessions() == []
        replay = asyncio.run(shared_registry().steer(self.task.pk, "last words", command_id="kept-1", wait=1))
        late = asyncio.run(shared_registry().steer(self.task.pk, "after the end", command_id="gone", wait=1))
        assert replay.as_payload() == accepted
        assert (late.outcome, late.code) == ("rejected", "session_closed")
