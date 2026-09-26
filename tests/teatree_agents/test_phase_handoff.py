"""A task that begins a new phase reads its predecessor's complete result from a durable file, on both lanes."""

import asyncio
import contextlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import pydantic_ai.models
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents._runner_options import SpawnOverrides, _build_options
from teatree.agents.claude_cli_spawn import APPEND_PROMPT_FILE_FLAG
from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.lane_b import toolsets as lane_b_toolsets
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.tool_names import TOOL_BASH, TOOL_READ
from teatree.agents.phase_handoff import delivered_phase_handoff
from teatree.agents.prompt import build_system_context
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import AutoReviewDispatch, PullRequest, Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.types import AdequacySection, PlanAdequacy
from tests.factories import planned_ticket

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

_HANDOFF_LINE = re.compile(r"^Complete predecessor result \(source of truth\): (?P<path>.+)$", re.MULTILINE)
_BOUNDARIES = (("planning", "coding"), ("coding", "testing"), ("testing", "reviewing"))
_RESULT = {
    "summary": "s" * 2500 + " the tail the inline bridge cuts",
    "files_modified": [f"src/module_{index}.py" for index in range(25)],
    "next_steps": [f"step {index}" for index in range(12)],
    "review_verdict": {"approved": False, "findings": ["unguarded write", "missing test"]},
    "evidence": ["https://example.com/pipelines/1"],
}


def _remove_tree(root: Path) -> None:
    def unlock(directory: Path) -> None:
        directory.chmod(0o700)
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                unlock(child)

    unlock(root)
    shutil.rmtree(root, ignore_errors=True)


class _Handoff(TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(_remove_tree, self.root)
        data_dir = patch("teatree.paths.DATA_DIR", self.root / "data")
        data_dir.start()
        self.addCleanup(data_dir.stop)
        self.ticket = self._ticket_with_worktree()

    def _ticket_with_worktree(self) -> Ticket:
        ticket = planned_ticket()
        worktree_dir = self.root / f"repo-{ticket.pk}"
        worktree_dir.mkdir()
        Worktree.objects.create(
            ticket=ticket, repo_path=str(worktree_dir), branch="feature", extra={"worktree_path": str(worktree_dir)}
        )
        return ticket

    def _child(
        self, parent_phase: str, child_phase: str, *, result: dict | None = None, ticket: Ticket | None = None
    ) -> Task:
        ticket = ticket or self.ticket
        parent = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, agent_id=parent_phase),
            phase=parent_phase,
            result_artifact_path="/artifacts/result.json",
        )
        TaskAttempt.objects.create(task=parent, result=result or _RESULT, artifact_path="/artifacts/attempt.log")
        return Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket, agent_id=child_phase),
            phase=child_phase,
            parent_task=parent,
        )

    @contextlib.contextmanager
    def _delivered(self, task: Task) -> Iterator[tuple[Path, str]]:
        with delivered_phase_handoff(task) as handoff:
            assert handoff is not None, "a task beginning a new phase after a recorded attempt must get a handoff"
            yield handoff, build_system_context(task, skills=[], handoff=handoff)

    @staticmethod
    def _handoff(context: str) -> Path:
        match = _HANDOFF_LINE.search(context)
        assert match is not None, "the fresh phase context names no predecessor handoff file"
        return Path(match["path"])


class TestEveryPhaseBoundaryHandsOverTheCompleteResult(_Handoff):
    def test_the_handoff_file_carries_the_whole_result_and_its_artifact_paths(self) -> None:
        for parent_phase, child_phase in _BOUNDARIES:
            with (
                self.subTest(boundary=f"{parent_phase}->{child_phase}"),
                self._delivered(self._child(parent_phase, child_phase)) as (_handoff, context),
            ):
                payload = json.loads(self._handoff(context).read_text(encoding="utf-8"))

                assert payload["result"] == _RESULT
                assert payload["artifact_path"] == "/artifacts/attempt.log"
                assert payload["result_artifact_path"] == "/artifacts/result.json"

    def test_the_handoff_survives_a_parent_block_the_context_budget_cuts(self) -> None:
        oversized = {**_RESULT, "summary": "x" * (MAX_APPEND_BYTES * 2)}

        with self._delivered(self._child("coding", "testing", result=oversized)) as (_handoff, context):
            assert len(context.encode()) <= MAX_APPEND_BYTES
            assert json.loads(self._handoff(context).read_text(encoding="utf-8"))["result"] == oversized

    def test_a_task_continuing_its_phase_gets_no_handoff(self) -> None:
        with delivered_phase_handoff(self._child("coding", "coding")) as handoff:
            assert handoff is None


class TestTheClaudeLaneCanReadTheHandoff(_Handoff):
    def test_the_spawn_is_granted_the_delivery_directory_and_never_the_store(self) -> None:
        for parent_phase, child_phase in _BOUNDARIES:
            child = self._child(parent_phase, child_phase)
            with self.subTest(boundary=f"{parent_phase}->{child_phase}"), self._delivered(child) as (handoff, context):
                options = _build_options(
                    child, context, phase=child_phase, skills=[], overrides=SpawnOverrides(handoff=handoff)
                )

                assert str(self._handoff(context).parent) in options.add_dirs
                assert str(handoff.parent.parent) not in options.add_dirs


class TestThePydanticLaneCanReadTheHandoff(_Handoff):
    @staticmethod
    def _read_through_lane_b(config: LaneBToolConfig, path: Path) -> list[str]:
        turns = iter(
            (
                ModelResponse(parts=[ToolCallPart(tool_name=TOOL_READ, args={"path": str(path)}, tool_call_id="read")]),
                ModelResponse(parts=[TextPart(content="read it")]),
            )
        )
        with patch.object(lane_b_toolsets, "build_mcp_toolsets", return_value=[]):
            toolsets = lane_b_toolsets.build_lane_b_toolsets(config).toolsets
        agent = Agent[None, str](FunctionModel(lambda _messages, _info: next(turns)), toolsets=toolsets)
        result = asyncio.run(agent.run("read the predecessor handoff"))
        return [
            str(part.content)
            for message in result.all_messages()
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]

    def test_the_jailed_read_tool_returns_the_whole_handoff(self) -> None:
        for parent_phase, child_phase in _BOUNDARIES:
            child = self._child(parent_phase, child_phase)
            with self.subTest(boundary=f"{parent_phase}->{child_phase}"), self._delivered(child) as (handoff, context):
                sdk_options = _build_options(
                    child, context, phase=child_phase, skills=[], overrides=SpawnOverrides(handoff=handoff)
                )
                config = LaneBToolConfig.from_options(HarnessOptions.from_sdk_options(sdk_options), phase=child_phase)

                returned = self._read_through_lane_b(config, self._handoff(context))

                assert [json.loads(content)["result"] for content in returned] == [_RESULT]


class TestTheHandoffPointerSurvivesTheBudgetBackstop(_Handoff):
    _SLUG = "souliane/teatree"
    _PR_ID = 4711

    def _reviewing_task_over_an_oversized_rubric(self) -> Task:
        delivering = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
        url = f"https://github.com/{self._SLUG}/pull/{self._PR_ID}"
        PullRequest.objects.create(
            ticket=delivering, overlay="t3-teatree", url=url, repo=self._SLUG, iid=str(self._PR_ID)
        )
        PlanArtifact.record(
            ticket=delivering,
            plan_text="grade an oversized rubric",
            recorded_by="planner",
            base_sha="f" * 40,
            adequacy=PlanAdequacy(
                design=AdequacySection(content="render every criterion"),
                integration_seams=AdequacySection(content=["src/teatree/agents/phase_blocks.py"]),
                edge_cases=AdequacySection(content=["a rubric larger than the context budget"]),
                test_strategy=AdequacySection(content="this module"),
                acceptance_criteria=AdequacySection(
                    content=[f"criterion {index}: " + "r" * (MAX_APPEND_BYTES // 2) for index in range(3)]
                ),
            ),
        )
        dispatch = AutoReviewDispatch.enqueue(
            slug=self._SLUG, pr_id=self._PR_ID, head_sha="e" * 40, pr_url=url, overlay="teatree"
        )
        assert dispatch is not None
        assert dispatch.task is not None
        testing = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="testing"
        )
        TaskAttempt.objects.create(task=testing, result=_RESULT)
        reviewing = dispatch.task
        reviewing.parent_task = testing
        reviewing.save(update_fields=["parent_task"])
        return reviewing

    def test_an_over_budget_rubric_cannot_cut_the_handoff_pointer(self) -> None:
        reviewing = self._reviewing_task_over_an_oversized_rubric()

        with self._delivered(reviewing) as (_handoff, context):
            assert len(context.encode()) <= MAX_APPEND_BYTES
            assert json.loads(self._handoff(context).read_text(encoding="utf-8"))["result"] == _RESULT


_OTHER_SECRET = "the other ticket's secret result"


def _sh(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=False)


def _handoff_named_in(append_file: str) -> Path:
    match = _HANDOFF_LINE.search(Path(append_file).read_text(encoding="utf-8"))
    assert match is not None, "the dispatched system context names no predecessor handoff file"
    return Path(match["path"])


class _ShellProbeClient:
    """A Claude SDK client whose turn runs same-uid shell commands, as the spawned CLI's Bash would."""

    def __init__(self, options, probe, *, fail: bool) -> None:
        self._options = options
        self._probe = probe
        self._fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        self._probe(_handoff_named_in(self._options.extra_args[APPEND_PROMPT_FILE_FLAG]))
        if self._fail:
            msg = "the agent died mid-run"
            raise RuntimeError(msg)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="done")], model="claude-opus-5")
        yield ResultMessage(
            subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=1, session_id="s1"
        )

    async def interrupt(self) -> None:
        return None


class TestTheClaudeDispatchCannotEscapeItsHandoff(_Handoff):
    def _dispatch(self, task: Task, probe=lambda _handoff: None, *, fail: bool = False) -> None:
        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(
                harness_mod, "ClaudeSDKClient", lambda **kwargs: _ShellProbeClient(kwargs["options"], probe, fail=fail)
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, _task: TaskUsage(0, 0.0))),
        ):
            run_agent(task, phase=task.phase, overlay_skill_metadata={})

    def test_its_shell_cannot_list_the_store_read_another_ticket_or_alter_its_own_handoff(self) -> None:
        self._dispatch(
            self._child("planning", "coding", result={"summary": _OTHER_SECRET}, ticket=self._ticket_with_worktree())
        )
        seen: dict[str, object] = {}

        def probe(handoff: Path) -> None:
            store = shlex.quote(str(handoff.parent.parent))
            seen["list"] = _sh(f"ls {store}")
            seen["read_other"] = _sh(f"cat {store}/*/*.json")
            seen["tamper"] = _sh(f"echo tampered >> {shlex.quote(str(handoff))}")
            seen["own"] = handoff.read_text(encoding="utf-8")

        self._dispatch(self._child("planning", "coding"), probe)

        assert seen["list"].returncode != 0
        assert _OTHER_SECRET not in seen["read_other"].stdout
        assert seen["tamper"].returncode != 0
        assert json.loads(seen["own"])["result"] == _RESULT

    def test_the_handoff_is_gone_once_the_run_ends(self) -> None:
        delivered: list[Path] = []

        self._dispatch(self._child("planning", "coding"), delivered.append)

        assert [path.exists() for path in delivered] == [False]

    def test_the_handoff_is_gone_when_the_run_fails(self) -> None:
        delivered: list[Path] = []

        with pytest.raises(RuntimeError):
            self._dispatch(self._child("planning", "coding"), delivered.append, fail=True)

        assert [path.exists() for path in delivered] == [False]


async def _adversarial_stream(seen: dict[str, str], messages, _info) -> AsyncIterator[object]:
    await asyncio.sleep(0)
    parts = [part for message in messages for part in getattr(message, "parts", [])]
    system = next(
        part.content for part in parts if isinstance(part, SystemPromptPart) and _HANDOFF_LINE.search(part.content)
    )
    handoff = _Handoff._handoff(system)
    store = shlex.quote(str(handoff.parent.parent))
    attacks = (
        ("list", TOOL_BASH, {"command": f"ls {store}"}),
        ("read_other", TOOL_BASH, {"command": f"cat {store}/*/*.json"}),
        ("tamper", TOOL_BASH, {"command": f"echo tampered >> {shlex.quote(str(handoff))}"}),
        ("own", TOOL_READ, {"path": str(handoff)}),
    )
    answers = [part for part in parts if isinstance(part, ToolReturnPart | RetryPromptPart)]
    seen["path"] = str(handoff)
    for (name, _tool, _args), answer in zip(attacks, answers, strict=False):
        seen[name] = str(answer.content)
    if len(answers) < len(attacks):
        name, tool, args = attacks[len(answers)]
        yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id=name)}
    else:
        yield "done"


class TestTheLaneBDispatchCannotEscapeItsHandoff(_Handoff):
    @staticmethod
    def _adversary(seen: dict[str, str]) -> FunctionModel:
        async def stream(messages, info) -> AsyncIterator[object]:
            async for chunk in _adversarial_stream(seen, messages, info):
                yield chunk

        return FunctionModel(stream_function=stream)

    def _dispatch(self, task: Task, model: FunctionModel) -> None:
        with (
            patch.object(
                runner_mod,
                "resolve_dispatch_harness",
                return_value=runner_mod.DispatchHarness(
                    harness=PydanticAiHarness(model=model, phase=task.phase), name="fake_harness", provider=None
                ),
            ),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, _task: TaskUsage(0, 0.0))),
            patch.object(lane_b_toolsets, "build_mcp_toolsets", return_value=[]),
        ):
            run_agent(task, phase=task.phase, overlay_skill_metadata={})

    def test_its_bash_cannot_list_the_store_read_another_ticket_or_alter_its_own_handoff(self) -> None:
        other: dict[str, str] = {}
        self._dispatch(
            self._child("planning", "coding", result={"summary": _OTHER_SECRET}, ticket=self._ticket_with_worktree()),
            self._adversary(other),
        )
        seen: dict[str, str] = {}

        self._dispatch(self._child("planning", "coding"), self._adversary(seen))

        assert not seen["list"].startswith("exit=0")
        assert _OTHER_SECRET not in seen["read_other"]
        assert not seen["tamper"].startswith("exit=0")
        assert json.loads(seen["own"])["result"] == _RESULT

    def test_the_handoff_is_gone_once_the_run_ends(self) -> None:
        seen: dict[str, str] = {}

        self._dispatch(self._child("planning", "coding"), self._adversary(seen))

        assert not Path(seen["path"]).exists()


class TestDeliveriesStayPrivateAndLeaveNothingBehind(_Handoff):
    def test_a_live_sibling_delivery_cannot_be_listed_or_globbed(self) -> None:
        other = self._child(
            "planning", "coding", result={"summary": _OTHER_SECRET}, ticket=self._ticket_with_worktree()
        )

        with (
            delivered_phase_handoff(other) as other_handoff,
            self._delivered(self._child("planning", "coding")) as (handoff, _),
        ):
            store = shlex.quote(str(handoff.parent.parent))
            listing = _sh(f"ls {store}")
            globbed = _sh(f"cat {store}/*/*.json")

        assert listing.returncode != 0
        assert other_handoff.parent.name not in listing.stdout
        assert _OTHER_SECRET not in globbed.stdout

    def test_a_delivery_the_agent_reopened_is_still_removed(self) -> None:
        with self._delivered(self._child("planning", "coding")) as (handoff, _):
            handoff.parent.chmod(0o700)
            nested = handoff.parent / "left-behind"
            nested.mkdir()
            (nested / "scratch.txt").write_text("scratch", encoding="utf-8")
            nested.chmod(0o500)

        assert not handoff.parent.exists()

    def test_a_delivery_that_cannot_be_removed_is_reported_loudly(self) -> None:
        with (
            self.assertLogs("teatree.agents.phase_handoff", level="ERROR") as logs,
            patch("teatree.agents.phase_handoff.shutil.rmtree", side_effect=OSError("device busy")),
            self._delivered(self._child("planning", "coding")) as (handoff, _),
        ):
            pass

        assert str(handoff.parent) in logs.output[0]
