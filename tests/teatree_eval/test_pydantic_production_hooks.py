# test-path: cross-cutting -- the direct-model eval backend must run shipped
# production PreToolUse hooks before its local tool stub executes.
"""Production-hook parity for the CLI-free pydantic/Anthropic eval transport."""

import asyncio
import dataclasses
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import HookEventMessage
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import FunctionToolCallEvent, PartStartEvent, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage

from teatree.agents.pydantic_ai_session import PydanticAiHarnessSession
from teatree.agents.pydantic_ai_turn import SessionRun
from teatree.eval.harness_failure import HOOKS_NOT_REGISTERED_REASON
from teatree.eval.models import AssistantTextMatcher, EvalSpec
from teatree.eval.production_hook_bridge import ProductionHookBridge, ProductionHookToolset
from teatree.eval.production_hooks import t3_plugin
from teatree.eval.pydantic_ai_runner import PydanticAiRunner
from teatree.eval.report import evaluate

_PROMPT = (
    "Handle PROJ-4521 and PROJ-4242. Binding rule: present a per-ticket plan "
    "in your response before any edit, commit, or push."
)
_PLAN = (
    "Plan — PROJ-4521: inspect forms.py, implement the literal-gate removal, "
    "run its tests, then verify before shipping.\n"
    "Plan — PROJ-4242: inspect views.py, implement the visibility filter, "
    "run its tests, then verify before shipping."
)


def _spec() -> EvalSpec:
    return EvalSpec(
        name="plan_first",
        scenario="plan before tools",
        agent_path="skills/code/SKILL.md",
        prompt=_PROMPT,
        matchers=(AssistantTextMatcher("contains", "Plan"),),
        source_path=Path(__file__),
        model="claude-sonnet-5",
        tools=("Bash", "AskUserQuestion"),
        production_hooks=True,
    )


def _toolset(executed: list[dict[str, object]]) -> FunctionToolset[None]:
    tools: FunctionToolset[None] = FunctionToolset()

    def bash(**kwargs: object) -> str:
        executed.append(dict(kwargs))
        return "executed"

    tools.add_function(bash, name="Bash")
    return tools


def _ctx(*, tool_call_id: str = "call-1") -> RunContext[None]:
    return RunContext(
        deps=None,
        model=TestModel(),
        usage=RunUsage(),
        tool_name="Bash",
        tool_call_id=tool_call_id,
        max_retries=1,
    )


def _fake_bridge(
    tmp_path: Path,
    scripts: list[tuple[str, str, int]],
    *,
    matcher: str = "Bash",
) -> ProductionHookBridge:
    plugin = tmp_path / "plugin"
    hooks = plugin / "hooks"
    hooks.mkdir(parents=True)
    commands: list[dict[str, object]] = []
    for name, body, timeout in scripts:
        script = tmp_path / name
        script.write_text(body, encoding="utf-8")
        commands.append({"type": "command", "command": f"{sys.executable} {script}", "timeout": timeout})
    (hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": commands}]}}),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    return ProductionHookBridge(_PROMPT, SessionRun.start(), tmp_path, state, plugin)


def test_live_manifest_denies_bash_first_and_never_executes_wrapped_tool(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(_spec(), run=run, state_root=tmp_path) as bridge:
        wrapped = ProductionHookToolset(_toolset(executed), bridge=bridge)
        bridge.observe(
            FunctionToolCallEvent(part=ToolCallPart(tool_name="Bash", args={"command": "true"}, tool_call_id="call-1"))
        )

        async def call() -> None:
            await wrapped.call_tool(
                "Bash",
                {"command": "true"},
                SimpleNamespace(tool_name="Bash", tool_call_id="call-1"),
                None,
            )

        with pytest.raises(ModelRetry, match="TEATREE PLAN-FIRST GATE"):
            asyncio.run(call())

        assert executed == []
        assert bridge.events
        assert bridge.events[-1].hook_event_name == "PreToolUse"
        assert bridge.events[-1].data["outcome"] == "block"
        assert bridge.events[-1].data["gate_id"] == "visible_plan_gate"
        assert bridge.events[-1].data["sequence"] == 1
        assert bridge.events[-1].data["tool_name"] == "Bash"
        assert bridge.events[-1].data["tool_use_id"] == "call-1"
        assert bridge.events[-1].data["assistant_text"] == ""


def test_visible_structured_plan_allows_wrapped_tool(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(_spec(), run=run, state_root=tmp_path) as bridge:
        bridge.observe(PartStartEvent(index=0, part=TextPart(_PLAN)))
        bridge.observe(
            FunctionToolCallEvent(part=ToolCallPart(tool_name="Bash", args={"command": "true"}, tool_call_id="call-1"))
        )
        wrapped = ProductionHookToolset(_toolset(executed), bridge=bridge)

        async def call() -> object:
            ctx = _ctx()
            tool = (await wrapped.get_tools(ctx))["Bash"]
            return await wrapped.call_tool(
                "Bash",
                {"command": "true"},
                ctx,
                tool,
            )

        assert asyncio.run(call()) == "executed"
        assert executed == [{"command": "true"}]
        assert bridge.events[-1].data["outcome"] == "allow"
        assert bridge.events[-1].data["sequence"] == 1
        assert bridge.events[-1].data["tool_name"] == "Bash"
        assert bridge.events[-1].data["tool_use_id"] == "call-1"
        assert bridge.events[-1].data["assistant_text"] == _PLAN


def test_hook_audit_snapshot_redacts_secret_assignments(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(_spec(), run=run, state_root=tmp_path) as bridge:
        bridge.observe(PartStartEvent(index=0, part=TextPart(f"{_PLAN}\nANTHROPIC_API_KEY=do-not-persist")))
        wrapped = ProductionHookToolset(_toolset(executed), bridge=bridge)

        async def call() -> object:
            ctx = _ctx(tool_call_id="redacted-call")
            tool = (await wrapped.get_tools(ctx))["Bash"]
            return await wrapped.call_tool("Bash", {"command": "true"}, ctx, tool)

        assert asyncio.run(call()) == "executed"
        audit_text = str(bridge.events[-1].data["assistant_text"])
        assert "do-not-persist" not in audit_text
        assert "ANTHROPIC_API_KEY=<redacted>" in audit_text


def test_runner_preserves_plan_visible_before_tool_when_final_only_refers_above() -> None:
    """The exact trial-2 shape must remain auditable after the temp bridge closes."""

    async def plan_tool_then_omit(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        if "ToolReturnPart" not in str(messages):
            yield _PLAN
            yield {0: DeltaToolCall(name="Bash", json_args='{"command":"true"}')}
        else:
            yield "Both tickets' plans are presented above. Which do you want?"

    run = PydanticAiRunner(model=FunctionModel(stream_function=plan_tool_then_omit)).run(_spec())

    assert evaluate(_spec(), run).passed is False
    assert len(run.gate_events) == 1
    event = run.gate_events[0]
    assert event.outcome == "allow"
    assert event.sequence == 1
    assert event.tool_name == "Bash"
    assert event.tool_use_id
    # An allow has no denying gate identity; the bridge must not invent one.
    assert event.gate_id == ""
    assert event.assistant_text == _PLAN
    assert run.text_blocks == ("Both tickets' plans are presented above. Which do you want?",)


def test_streamed_session_denial_is_recorded_and_model_can_recover(tmp_path: Path) -> None:
    async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        denied = "TEATREE PLAN-FIRST GATE" in str(messages)
        if not denied:
            yield {0: DeltaToolCall(name="Bash", json_args='{"command":"true"}')}
        else:
            yield _PLAN

    run = SessionRun.start()
    with ProductionHookBridge.for_spec(_spec(), run=run, state_root=tmp_path) as bridge:
        agent: Agent[None, str] = Agent(
            FunctionModel(stream_function=stream_fn),
            toolsets=[ProductionHookToolset(_toolset([]), bridge=bridge)],
        )
        session = PydanticAiHarnessSession(
            agent,
            model_name="offline-model",
            run=run,
        )
        session.observe_transport_hooks(bridge.observe, bridge.events)

        async def drive() -> list[object]:
            await session.query(_PROMPT)
            return [message async for message in session.receive_response()]

        messages = asyncio.run(drive())
        bridge.flush_transcript()
        transcript = [json.loads(line) for line in bridge.transcript_path.read_text(encoding="utf-8").splitlines()]

    hooks = [message for message in messages if isinstance(message, HookEventMessage)]
    assert hooks
    assert hooks[0].hook_event_name == "PreToolUse"
    assert hooks[0].data["outcome"] == "block"
    assert "visible_plan_gate" in str(hooks[0].data)
    assert [entry["message"]["role"] for entry in transcript] == ["user", "assistant", "user", "assistant"]
    assert transcript[1]["message"]["content"][0]["name"] == "Bash"
    assert transcript[2]["message"]["content"][0]["is_error"] is True
    assert transcript[3]["message"]["content"][0]["text"] == _PLAN


def test_denied_task_create_retry_with_visible_plan_executes_once(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []

    async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        if "task-created" in str(messages):
            yield "done"
            return
        denied = "TEATREE PLAN-FIRST GATE" in str(messages)
        if denied:
            yield _PLAN
        yield {
            0: DeltaToolCall(
                name="TaskCreate",
                json_args='{"subject":"Plan both tickets","description":"Keep TODO-4 and TODO-6 separate"}',
            )
        }

    tools: FunctionToolset[None] = FunctionToolset()

    def task_create(**kwargs: object) -> str:
        executed.append(dict(kwargs))
        return "task-created"

    tools.add_function(task_create, name="TaskCreate")
    spec = dataclasses.replace(_spec(), tools=("TaskCreate",))
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(spec, run=run, state_root=tmp_path) as bridge:
        agent: Agent[None, str] = Agent(
            FunctionModel(stream_function=stream_fn),
            toolsets=[ProductionHookToolset(tools, bridge=bridge)],
        )
        session = PydanticAiHarnessSession(agent, model_name="offline-model", run=run)
        session.observe_transport_hooks(bridge.observe, bridge.events)

        async def drive() -> list[object]:
            await session.query(_PROMPT)
            return [message async for message in session.receive_response()]

        messages = asyncio.run(drive())

    hooks = [message for message in messages if isinstance(message, HookEventMessage)]
    assert [hook.data["outcome"] for hook in hooks] == ["block", "allow"]
    assert hooks[0].data["assistant_text"] == ""
    assert hooks[1].data["assistant_text"] == _PLAN
    assert executed == [{"subject": "Plan both tickets", "description": "Keep TODO-4 and TODO-6 separate"}]


def test_two_denied_task_create_corrections_can_recover_once(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []

    async def stream_fn(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        if "task-created" in str(messages):
            yield "done"
            return
        if str(messages).count("TEATREE PLAN-FIRST GATE") >= 2:
            yield _PLAN
        yield {0: DeltaToolCall(name="TaskCreate", json_args='{"subject":"Plan","description":"Both"}')}

    tools: FunctionToolset[None] = FunctionToolset()

    def task_create(**kwargs: object) -> str:
        executed.append(dict(kwargs))
        return "task-created"

    tools.add_function(task_create, name="TaskCreate")
    spec = dataclasses.replace(_spec(), tools=("TaskCreate",))
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(spec, run=run, state_root=tmp_path) as bridge:
        agent: Agent[None, str] = Agent(
            FunctionModel(stream_function=stream_fn),
            toolsets=[ProductionHookToolset(tools, bridge=bridge)],
        )
        session = PydanticAiHarnessSession(agent, model_name="offline-model", run=run)
        session.observe_transport_hooks(bridge.observe, bridge.events)

        async def drive() -> list[object]:
            await session.query(_PROMPT)
            return [message async for message in session.receive_response()]

        messages = asyncio.run(drive())

    hooks = [message for message in messages if isinstance(message, HookEventMessage)]
    assert [hook.data["outcome"] for hook in hooks] == ["block", "block", "allow"]
    assert executed == [{"subject": "Plan", "description": "Both"}]


def test_repeated_plan_gate_noncompliance_is_denied_until_the_breaker_relieves_it(tmp_path: Path) -> None:
    """Relief comes from the deny-circuit breaker, never from the gate passing quietly.

    The plan gate is a workflow gate on the breaker's UX tier, so the K-th
    identical denial deliberately fails that one call open rather than escalating
    a refusal an agent has no way to answer.
    """
    executed: list[dict[str, object]] = []

    async def tool_only(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        yield {0: DeltaToolCall(name="TaskCreate", json_args='{"subject":"Plan","description":"Both"}')}

    tools: FunctionToolset[None] = FunctionToolset()

    def task_create(**kwargs: object) -> str:
        executed.append(dict(kwargs))
        return "must-not-run"

    tools.add_function(task_create, name="TaskCreate")
    spec = dataclasses.replace(_spec(), tools=("TaskCreate",))
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(spec, run=run, state_root=tmp_path) as bridge:
        agent: Agent[None, str] = Agent(
            FunctionModel(stream_function=tool_only),
            toolsets=[ProductionHookToolset(tools, bridge=bridge)],
        )
        session = PydanticAiHarnessSession(agent, model_name="offline-model", run=run)
        session.observe_transport_hooks(bridge.observe, bridge.events)

        async def drive() -> list[object]:
            await session.query(_PROMPT)
            return [message async for message in session.receive_response()]

        messages = asyncio.run(drive())

    outcomes = [message.data["outcome"] for message in messages if isinstance(message, HookEventMessage)]

    assert outcomes[:2] == ["block", "block"]
    assert "allow" in outcomes[2:]
    assert executed, "the breaker's fail-open is the only thing that may let the wrapped tool run"


def test_non_hook_tool_retry_keeps_the_standard_one_retry_budget(tmp_path: Path) -> None:
    attempts = 0

    async def planned_tool(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        yield _PLAN
        yield {0: DeltaToolCall(name="TaskCreate", json_args='{"subject":"Plan","description":"Both"}')}

    tools: FunctionToolset[None] = FunctionToolset()

    def task_create(**_kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        message = "ordinary tool failure"
        raise ModelRetry(message)

    tools.add_function(task_create, name="TaskCreate")
    spec = dataclasses.replace(_spec(), tools=("TaskCreate",))
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(spec, run=run, state_root=tmp_path) as bridge:
        agent: Agent[None, str] = Agent(
            FunctionModel(stream_function=planned_tool),
            toolsets=[ProductionHookToolset(tools, bridge=bridge)],
        )
        session = PydanticAiHarnessSession(agent, model_name="offline-model", run=run)
        session.observe_transport_hooks(bridge.observe, bridge.events)

        async def drive() -> list[object]:
            await session.query(_PROMPT)
            return [message async for message in session.receive_response()]

        asyncio.run(drive())

    assert attempts == 2


def test_production_hooks_accept_a_text_only_run_without_synthesizing_an_event() -> None:
    async def text_only(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        yield "done"

    spec = dataclasses.replace(_spec(), matchers=(AssistantTextMatcher("contains", "done"),))
    run = PydanticAiRunner(model=FunctionModel(stream_function=text_only)).run(spec)

    assert evaluate(spec, run).passed
    assert run.terminal_reason == "success"


def test_unrelated_prompt_fails_open(tmp_path: Path) -> None:
    spec = dataclasses.replace(_spec(), prompt="Run the tests for PROJ-4521.")
    executed: list[dict[str, object]] = []
    run = SessionRun.start()
    with ProductionHookBridge.for_spec(spec, run=run, state_root=tmp_path) as bridge:
        wrapped = ProductionHookToolset(_toolset(executed), bridge=bridge)

        async def call() -> object:
            ctx = _ctx()
            tool = (await wrapped.get_tools(ctx))["Bash"]
            return await wrapped.call_tool("Bash", {"command": "true"}, ctx, tool)

        assert asyncio.run(call()) == "executed"
        assert executed == [{"command": "true"}]


def test_direct_hook_environment_isolates_host_context_and_model_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-hook")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-reach-hook")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/host/config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/claude")
    bridge = _fake_bridge(tmp_path, [])

    env = bridge._hook_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["HOME"] == str(bridge.state_root)
    assert env["XDG_CONFIG_HOME"] == str(bridge.state_root / ".config")
    assert env["CLAUDE_CONFIG_DIR"] == str(bridge.state_root / ".claude")


def test_unmatched_manifest_tool_executes_without_synthesizing_an_event(tmp_path: Path) -> None:
    bridge = _fake_bridge(tmp_path, [("allow.py", "print('{}')", 2)], matcher="Edit")
    assert bridge.pre_tool_use("Bash", {"command": "true"}) is None
    assert bridge.events == []


def test_malformed_success_output_fails_open_but_is_recorded(tmp_path: Path) -> None:
    bridge = _fake_bridge(tmp_path, [("malformed.py", "print('not-json')", 2)])
    assert bridge.pre_tool_use("Bash", {"command": "true"}) is None
    assert bridge.events[-1].data["outcome"] == "allow"
    assert bridge.events[-1].data["exit_code"] == 0


def test_timeout_fails_open_but_is_recorded(tmp_path: Path) -> None:
    bridge = _fake_bridge(tmp_path, [("slow.py", "import time\ntime.sleep(5)", 1)])
    assert bridge.pre_tool_use("Bash", {"command": "true"}) is None
    assert bridge.events[-1].data["outcome"] == "error"
    assert bridge.events[-1].data["exit_code"] is None
    assert "TimeoutExpired" in str(bridge.events[-1].data["output"])


def test_multi_hook_manifest_runs_in_order_and_stops_at_first_deny(tmp_path: Path) -> None:
    marker = tmp_path / "order.txt"
    deny = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "denied second",
                "gate_id": "second",
            }
        }
    )
    second_body = "\n".join(
        (
            "from pathlib import Path",
            f"p=Path({str(marker)!r})",
            "p.write_text(p.read_text()+'second\\n')",
            f"print({deny!r})",
            "raise SystemExit(2)",
        )
    )
    bridge = _fake_bridge(
        tmp_path,
        [
            ("first.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('first\\n')", 2),
            ("second.py", second_body, 2),
            (
                "third.py",
                f"from pathlib import Path\np=Path({str(marker)!r})\np.write_text(p.read_text()+'third\\n')",
                2,
            ),
        ],
    )

    assert bridge.pre_tool_use("Bash", {"command": "true"}) == "denied second"
    assert marker.read_text(encoding="utf-8") == "first\nsecond\n"
    assert [event.data["outcome"] for event in bridge.events] == ["allow", "block"]
    assert [event.data["sequence"] for event in bridge.events] == [1, 1]
    assert [event.data["tool_use_id"] for event in bridge.events] == ["direct-1", "direct-1"]
    assert bridge.events[-1].data["gate_id"] == "second"


def test_direct_bridge_and_sdk_backend_resolve_the_same_shipped_plugin(tmp_path: Path) -> None:
    with ProductionHookBridge.for_spec(_spec(), run=SessionRun.start(), state_root=tmp_path) as bridge:
        assert bridge.plugin_root == Path(t3_plugin()["path"])


class TestHookExitCodeIsClassified:
    """A hook that neither allowed nor denied must not be scored as a sanctioned allow.

    The outcome was ``block`` or ``allow`` and nothing else, so a router crash
    (exit 1) read as a gate that examined the call and let it through — the
    strongest possible reading of the weakest possible evidence.
    """

    @staticmethod
    def _outcome(tmp_path: Path, script: str) -> str:
        bridge = _fake_bridge(tmp_path, [("hook.py", script, 10)])
        bridge.pre_tool_use("Bash", {"command": "true"})
        return str(bridge.events[-1].data["outcome"])

    def test_a_crashed_hook_is_an_error(self, tmp_path: Path) -> None:
        assert self._outcome(tmp_path, "import sys\nsys.exit(1)\n") == "error"

    def test_an_unexpected_exit_code_is_an_error(self, tmp_path: Path) -> None:
        assert self._outcome(tmp_path, "import sys\nsys.exit(7)\n") == "error"

    def test_a_clean_exit_is_still_an_allow(self, tmp_path: Path) -> None:
        assert self._outcome(tmp_path, "pass\n") == "allow"

    def test_the_deny_exit_is_still_a_block(self, tmp_path: Path) -> None:
        assert self._outcome(tmp_path, "import sys\nsys.exit(2)\n") == "block"


class TestUnregisteredPluginFailsLoud:
    """A hooked run that gated calls yet captured no hook event measured the raw model.

    The pydantic lane only ever fires a hook from a tool call, so "zero hook
    events" alone cannot carry the SDK lane's unconditional guard: a text-only
    trajectory would read as an unregistered plugin. The bridge's call counter is
    what separates the two.
    """

    @staticmethod
    def _unregistered_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        hooks = tmp_path / "plugin" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        monkeypatch.setattr(
            "teatree.eval.production_hook_bridge.preflighted_plugin_root",
            lambda: tmp_path / "plugin",
        )

    @staticmethod
    async def _tool_then_text(messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        if "ToolReturnPart" in str(messages):
            yield _PLAN
        else:
            yield {0: DeltaToolCall(name="Bash", json_args='{"command":"true"}')}

    @staticmethod
    async def _text_only(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        yield _PLAN

    def test_gated_calls_with_no_hook_event_are_a_harness_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unregistered_plugin(tmp_path, monkeypatch)

        run = PydanticAiRunner(model=FunctionModel(stream_function=self._tool_then_text)).run(_spec())

        assert run.terminal_reason == HOOKS_NOT_REGISTERED_REASON
        assert run.is_error is True

    def test_a_trajectory_with_no_tool_call_is_graded_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._unregistered_plugin(tmp_path, monkeypatch)

        run = PydanticAiRunner(model=FunctionModel(stream_function=self._text_only)).run(_spec())

        assert run.terminal_reason != HOOKS_NOT_REGISTERED_REASON
        assert evaluate(_spec(), run).passed is True

    def test_a_registered_plugin_that_fired_is_graded_normally(self) -> None:
        run = PydanticAiRunner(model=FunctionModel(stream_function=self._tool_then_text)).run(_spec())

        assert run.terminal_reason != HOOKS_NOT_REGISTERED_REASON
        assert run.gate_events
