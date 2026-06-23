"""In-process ``claude-agent-sdk`` eval runner.

The runner drives ``claude_agent_sdk.query`` per scenario, collects the typed
messages, and produces an :class:`~teatree.eval.models.EvalRun` byte-identical in
shape to the deleted ``claude -p`` runner. Grading (report.py) is unchanged, so
the swap is invisible to the grader. The SDK is mocked here — no metered calls.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from teatree.eval.models import (
    DEFAULT_MAX_TURNS,
    AnyOf,
    EvalSpec,
    ExpectItem,
    FinalStateMatcher,
    Matcher,
    TokenUsage,
    canonicalize_tool,
)
from teatree.eval.sdk_runner import (
    CLAUDE_RESOLVE_MAX_ATTEMPTS,
    DEFAULT_WATCHDOG_SECONDS,
    MAX_BUDGET_USD,
    WATCHDOG_SECONDS,
    BudgetExceededError,
    ClaudeCliMissingError,
    CleanRoomConfig,
    SdkInProcessRunner,
    build_sdk_options,
    classify_terminal_error,
    is_success_result_error,
    resolve_claude_path,
)
from teatree.eval.system_prompt_file import resolve_system_prompt, spill_system_prompt
from teatree.eval.toolset import (
    DELEGATION_SUBAGENT_NAME,
    KNOWN_BUILTIN_TOOLS,
    SUBAGENT_SPAWN_TOOL,
    build_delegation_agents,
    compute_available_tools,
    compute_disallowed_tools,
    scenario_exposes_subagent_spawn,
)
from teatree.eval.transcript import _USAGE_KEY_TO_FIELD


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _spec(  # noqa: PLR0913 — test-data builder: each kwarg maps 1:1 to an EvalSpec field a case varies.
    tmp_path: Path,
    *,
    max_turns: int = 3,
    model: str = "haiku",
    tools: tuple[str, ...] = ("Bash",),
    max_budget_usd: float | None = None,
    watchdog_seconds: float | None = None,
    lane: str = "clean_room",
) -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="worktree_first",
        scenario="agent must create a worktree first",
        agent_path=str(agent),
        prompt="Fix README.md typo.",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=tmp_path / "spec.yaml",
        model=model,
        max_turns=max_turns,
        tools=tools,
        max_budget_usd=max_budget_usd,
        watchdog_seconds=watchdog_seconds,
        lane=lane,
    )


def _fake_query(messages: list[Any]):
    """Build a stand-in for ``claude_agent_sdk.query`` yielding *messages*.

    The real ``query`` is an async generator keyword-only on ``prompt``; the
    runner consumes it via ``asyncio.run``. The stub records the options it was
    called with so isolation/clean-room assertions can inspect them.
    """
    captured: dict[str, Any] = {}

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        captured["prompt"] = prompt
        captured["options"] = options
        # The clean-room options spill the system prompt to a --system-prompt-file
        # under the isolated cwd, which is deleted when the context exits; resolve
        # it to text HERE, while the file still exists, so post-hoc assertions can
        # inspect the actual prompt content the CLI receives.
        captured["system_prompt_text"] = resolve_system_prompt(options.system_prompt) if options else ""
        for message in messages:
            yield message

    return _query, captured


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _result(  # noqa: PLR0913 — test-data builder: each kwarg maps 1:1 to a ResultMessage field a case varies.
    *,
    subtype: str = "success",
    is_error: bool = False,
    total_cost_usd: float | None = 0.0123,
    num_turns: int = 2,
    usage: dict[str, Any] | None = None,
    model_usage: dict[str, Any] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=10,
        duration_api_ms=8,
        is_error=is_error,
        num_turns=num_turns,
        session_id="s1",
        total_cost_usd=total_cost_usd,
        usage=usage,
        model_usage=model_usage,
        result="ok",
    )


class TestSdkInProcessRunnerSkip:
    def test_returns_skip_run_when_claude_missing(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with patch("teatree.eval.sdk_runner.shutil.which", return_value=None):
            result = SdkInProcessRunner().run(spec)
        assert result.terminal_reason.startswith("skipped:")
        assert result.is_error is False
        assert result.tool_calls == ()

    def test_require_executed_hard_errors_when_claude_missing(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value=None),
            pytest.raises(ClaudeCliMissingError),
        ):
            SdkInProcessRunner(require_executed=True).run(spec)


class TestSdkInProcessRunnerCapture:
    def _run(self, spec: EvalSpec, messages: list[Any], **kwargs: Any):
        query, captured = _fake_query(messages)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner(**kwargs).run(spec)
        return run, captured

    def test_captures_tool_calls_text_terminal_cost(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[
                    TextBlock(text="Creating a worktree first."),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"}),
                ],
                model="haiku",
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="t2", name="Bash", input={"command": "echo done"})],
                model="haiku",
            ),
            _result(total_cost_usd=0.0456),
        ]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "success"
        assert run.is_error is False
        assert len(run.tool_calls) == 2
        assert run.tool_calls[0].name == "Bash"
        assert run.tool_calls[0].input["command"].startswith("git worktree add")
        assert run.tool_calls[0].turn == 1
        assert run.tool_calls[1].turn == 2
        assert run.text_blocks == ("Creating a worktree first.",)
        assert run.cost_usd == pytest.approx(0.0456)

    def test_subagent_sidechain_tool_calls_are_not_attributed_to_main_agent(self, tmp_path: Path) -> None:
        # The SDK streams a dispatched sub-agent's turns inline into the same query
        # output, each tagged with parent_tool_use_id. Only the MAIN agent's call
        # (the Agent dispatch, parent_tool_use_id None) is captured; the sub-agent's
        # worktree .py Edit (parent set) is excluded (#2596).
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="d1", name="Agent", input={"prompt": "fix it in a worktree"})],
                model="haiku",
            ),
            AssistantMessage(
                content=[ToolUseBlock(id="s1", name="Edit", input={"file_path": "/tmp/wt/x.py"})],
                model="haiku",
                parent_tool_use_id="d1",
            ),
            _result(total_cost_usd=0.01),
        ]
        run, _ = self._run(spec, messages)
        assert [c.name for c in run.tool_calls] == ["Agent"], (
            "the sub-agent's .py Edit (parent_tool_use_id set) leaked into the main-agent tool calls"
        )

    def test_captures_usage_and_billed_model(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [
            _result(
                total_cost_usd=0.0456,
                usage={
                    "input_tokens": 120,
                    "cache_creation_input_tokens": 340,
                    "cache_read_input_tokens": 6500,
                    "output_tokens": 80,
                },
                model_usage={"claude-opus-4-8": {"input_tokens": 6960, "output_tokens": 80}},
            ),
        ]
        run, _ = self._run(spec, messages)
        assert run.usage == TokenUsage(input=120, cache_creation=340, cache_read=6500, output=80)
        assert run.billed_model == "claude-opus-4-8"

    def test_run_without_usage_has_all_zero_usage_and_no_billed_model(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        run, _ = self._run(spec, [_result(total_cost_usd=None)])
        assert run.usage == TokenUsage()
        assert run.billed_model is None

    def test_haiku_aux_alongside_requested_model_is_not_a_fallback(self, tmp_path: Path) -> None:
        # The PROVEN bug: Claude Code always runs claude-haiku-4-5 as a cheap aux
        # model; haiku winning token volume must NOT flag fell_back.
        spec = _spec(tmp_path, model="claude-opus-4-8")
        messages = [
            _result(
                total_cost_usd=0.52,
                model_usage={
                    "claude-haiku-4-5-20251001": {"costUSD": 0.02, "inputTokens": 9000, "outputTokens": 40},
                    "claude-opus-4-8": {"costUSD": 0.5, "inputTokens": 80, "outputTokens": 200},
                },
            ),
        ]
        run, _ = self._run(spec, messages)
        assert run.fell_back is False
        assert run.main_cost_usd == pytest.approx(0.5)
        assert run.aux_cost_usd == pytest.approx(0.02)

    def test_requested_model_substituted_is_a_fallback(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="claude-opus-4-8")
        messages = [_result(total_cost_usd=0.3, model_usage={"claude-sonnet-4-6": {"costUSD": 0.3}})]
        run, _ = self._run(spec, messages)
        assert run.fell_back is True

    def test_unobservable_model_usage_is_not_a_fallback(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="claude-opus-4-8")
        run, _ = self._run(spec, [_result(total_cost_usd=None)])
        assert run.fell_back is None
        assert run.main_cost_usd == pytest.approx(0.0)
        assert run.aux_cost_usd == pytest.approx(0.0)

    def test_error_result_marks_is_error(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [_result(subtype="error_max_turns", is_error=True, total_cost_usd=0.01)]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "error_max_turns"
        assert run.is_error is True

    def test_no_result_message_is_aborted(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        messages = [AssistantMessage(content=[TextBlock(text="hi")], model="haiku")]
        run, _ = self._run(spec, messages)
        assert run.terminal_reason == "aborted"
        assert run.is_error is True

    def test_zero_cost_metered_run_records_zero(self, tmp_path: Path) -> None:
        # The unmetered-sdk guard relies on cost_usd==0.0 surfacing here.
        spec = _spec(tmp_path)
        messages = [_result(total_cost_usd=None)]
        run, _ = self._run(spec, messages)
        assert run.cost_usd == pytest.approx(0.0)

    def test_max_turns_override_takes_precedence(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, max_turns=3)
        _run, captured = self._run(spec, [_result()], max_turns_override=9)
        assert captured["options"].max_turns == 9

    def test_spec_max_turns_used_without_override(self, tmp_path: Path) -> None:
        # A declared value AT/ABOVE the clean-room floor is used verbatim. The
        # declared budget is derived from the floor (floor + 5) so this stays
        # genuinely above-floor across any future floor recalibration.
        from teatree.eval.models import CLEAN_ROOM_MIN_TURNS  # noqa: PLC0415

        declared = CLEAN_ROOM_MIN_TURNS + 5
        spec = _spec(tmp_path, max_turns=declared)
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].max_turns == declared

    def test_prompt_and_model_and_tools_flow_to_options(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="haiku", tools=("Bash", "Read"))
        _run, captured = self._run(spec, [_result()])
        assert captured["prompt"] == "Fix README.md typo."
        assert captured["options"].model == "haiku"
        assert list(captured["options"].allowed_tools) == ["Bash", "Read"]

    def test_budget_breaker_is_set(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_max_budget_usd_override_flows_to_options(self, tmp_path: Path) -> None:
        # A non-default per-run budget (the benchmark's generous cap) threads from
        # the runner through CleanRoomConfig into ClaudeAgentOptions.max_budget_usd.
        spec = _spec(tmp_path)
        _run, captured = self._run(spec, [_result()], max_budget_usd=2.0)
        assert captured["options"].max_budget_usd == pytest.approx(2.0)

    def test_per_scenario_budget_overrides_the_run_budget(self, tmp_path: Path) -> None:
        # A scenario's own max_budget_usd (a delegation scenario's cap RELIEF that
        # FITS a legitimate sub-agent TDD cycle) takes precedence over the run-level
        # budget — so the correct, costlier trajectory is measured, not truncated.
        spec = _spec(tmp_path, max_budget_usd=4.0)
        _run, captured = self._run(spec, [_result()], max_budget_usd=1.0)
        assert captured["options"].max_budget_usd == pytest.approx(4.0)

    def test_run_budget_used_when_scenario_declares_none(self, tmp_path: Path) -> None:
        # Backward compatibility: a scenario WITHOUT a per-scenario budget defers to
        # the run-level budget exactly as before.
        spec = _spec(tmp_path, max_budget_usd=None)
        _run, captured = self._run(spec, [_result()], max_budget_usd=1.0)
        assert captured["options"].max_budget_usd == pytest.approx(1.0)

    def _capture_watchdog_timeout(self, spec: EvalSpec, tmp_path: Path) -> float | None:
        """Run *spec* with ``asyncio.wait_for`` spied, returning the timeout it was driven under.

        The spy forwards via ``**kwargs`` (it must NOT declare a ``timeout``
        parameter — that trips ASYNC109) so the real ``wait_for`` keyword still
        flows through while the captured value is recorded.
        """
        captured: dict[str, Any] = {}
        real_wait_for = asyncio.wait_for

        async def _spy(awaitable: Any, **kwargs: Any) -> Any:
            captured["timeout"] = kwargs.get("timeout")
            return await real_wait_for(awaitable, **kwargs)

        query, _ = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
            patch("teatree.eval.sdk_runner.WATCHDOG_SECONDS", 12.5),
            patch("teatree.eval.sdk_runner.asyncio.wait_for", _spy),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        return captured.get("timeout")

    def test_per_scenario_watchdog_overrides_the_lane_default(self, tmp_path: Path) -> None:
        # A scenario's own watchdog_seconds (cap RELIEF for a longer sub-agent TDD
        # cycle) takes precedence over the lane default WATCHDOG_SECONDS — the timeout
        # the run is driven under is the scenario's (600), not the shared default (12.5).
        spec = _spec(tmp_path, watchdog_seconds=600.0)
        assert self._capture_watchdog_timeout(spec, tmp_path) == pytest.approx(600.0)

    def test_lane_watchdog_used_when_scenario_declares_none(self, tmp_path: Path) -> None:
        # Backward compatibility: a scenario WITHOUT a per-scenario watchdog defers to
        # the lane default WATCHDOG_SECONDS (12.5 under the patch).
        spec = _spec(tmp_path, watchdog_seconds=None)
        assert self._capture_watchdog_timeout(spec, tmp_path) == pytest.approx(12.5)

    def test_model_at_effort_tag_splits_into_model_and_effort_options(self, tmp_path: Path) -> None:
        # ClaudeAgentOptions.effort is the SDK's first-class reasoning-effort
        # field (the transport renders it as the CLI's `--effort <level>` flag).
        spec = _spec(tmp_path, model="claude-opus-4-8@xhigh")
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].model == "claude-opus-4-8"
        assert captured["options"].effort == "xhigh"

    def test_plain_model_leaves_effort_unset(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, model="claude-fable-5")
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].model == "claude-fable-5"
        assert captured["options"].effort is None

    def test_lane_effort_reaches_the_clean_room_when_scenario_declares_none(self, tmp_path: Path) -> None:
        # The metered lane's representative effort (`--effort high` → the runner's
        # `effort=` kwarg) reaches CleanRoomConfig.effort and the SDK options when
        # the scenario declares no `@effort` of its own.
        spec = _spec(tmp_path, model="claude-fable-5")
        _run, captured = self._run(spec, [_result()], effort="high")
        assert captured["options"].effort == "high"

    def test_scenario_declared_effort_wins_over_the_lane_default(self, tmp_path: Path) -> None:
        # A scenario's own `model@effort` is authoritative: the lane-level default
        # must NOT override an explicitly declared scenario effort.
        spec = _spec(tmp_path, model="claude-opus-4-8@xhigh")
        _run, captured = self._run(spec, [_result()], effort="high")
        assert captured["options"].effort == "xhigh"

    def test_no_lane_effort_leaves_a_plain_model_at_default_effort(self, tmp_path: Path) -> None:
        # Backward compatibility: without a lane effort, a plain model stays at the
        # model's default effort (effort=None), exactly as before.
        spec = _spec(tmp_path, model="claude-fable-5")
        _run, captured = self._run(spec, [_result()])
        assert captured["options"].effort is None

    def test_timeout_yields_timeout_run(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)

        async def _slow_query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            raise TimeoutError
            yield  # pragma: no cover

        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", _slow_query),
            patch("teatree.eval.sdk_runner.WATCHDOG_SECONDS", 0.01),
        ):
            run = SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert run.terminal_reason == "timeout"
        assert run.is_error is True


class TestUsageSchemaConformance:
    """Fail loud if the SDK ``ResultMessage.usage`` wire keys drift (#2192).

    Cost observability is keyed on four ``usage`` keys. If a future SDK renames
    or drops one, the round-trip below silently zeroes that token class — a
    silent loss of the cache-cost signal. This pins the contract so the drift is
    a RED test, not an invisible regression.
    """

    _WIRE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")

    def test_extractor_mapping_names_exactly_the_four_wire_keys(self) -> None:
        assert tuple(key for key, _ in _USAGE_KEY_TO_FIELD) == self._WIRE_KEYS

    def test_result_message_carries_a_usage_field(self) -> None:
        # The SDK type itself must keep a ``usage`` slot — the runner reads it.
        message = _result(usage=dict.fromkeys(self._WIRE_KEYS, 1))
        assert message.usage == dict.fromkeys(self._WIRE_KEYS, 1)

    def test_representative_usage_round_trips_through_the_runner(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        usage = {key: i + 1 for i, key in enumerate(self._WIRE_KEYS)}
        messages = [_result(usage=usage, model_usage={"claude-opus-4-8": usage})]
        query, _ = _fake_query(messages)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner().run(spec)
        assert run.usage == TokenUsage(input=1, cache_creation=2, cache_read=3, output=4)


class TestSdkInProcessRunnerAgentDefinition:
    def _run(self, spec: EvalSpec, **kwargs: Any):
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(**kwargs).run(spec)
        return captured

    def test_raises_when_agent_definition_missing(self, tmp_path: Path) -> None:
        spec = EvalSpec(
            name="bad",
            scenario="bad",
            agent_path=str(tmp_path / "does-not-exist.md"),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(FileNotFoundError),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)

    def test_raises_when_agent_definition_empty(self, tmp_path: Path) -> None:
        agent = tmp_path / "empty.md"
        agent.write_text("", encoding="utf-8")
        spec = EvalSpec(
            name="empty",
            scenario="empty",
            agent_path=str(agent),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(ValueError, match="empty"),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)

    def test_agent_sections_send_only_named_section_as_system_prompt(self, tmp_path: Path) -> None:
        agent = tmp_path / "rules.md"
        agent.write_text(
            "# Agent Rules\n\nframing\n\n"
            "## Background Long Operations\n\nBackground >15s work.\n\n"
            "## Unrelated Other Rule\n\nfifty other rules here.\n",
            encoding="utf-8",
        )
        spec = EvalSpec(
            name="bg",
            scenario="background long ops",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
            agent_sections=("Background Long Operations",),
        )
        captured = self._run(spec, workspace=tmp_path)
        system_prompt = captured["system_prompt_text"]
        assert "Background >15s work." in system_prompt
        assert "fifty other rules here." not in system_prompt

    def test_no_agent_sections_sends_whole_file(self, tmp_path: Path) -> None:
        agent = tmp_path / "rules.md"
        full = "# Agent Rules\n\n## A\n\naaa\n\n## B\n\nbbb\n"
        agent.write_text(full, encoding="utf-8")
        spec = EvalSpec(
            name="full",
            scenario="full file",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
        )
        captured = self._run(spec, workspace=tmp_path)
        from teatree.eval.prompt_framing import LIVE_ENV_FRAMING  # noqa: PLC0415

        assert captured["system_prompt_text"] == full + LIVE_ENV_FRAMING

    def test_runner_appends_live_environment_framing(self, tmp_path: Path) -> None:
        # The clean-room runner appends the live-environment framing so the model
        # issues the tool call instead of narrating it as text. The framing is the
        # runner's lever only — the judge path keeps its rubric system prompt.
        from teatree.eval.prompt_framing import LIVE_ENV_FRAMING  # noqa: PLC0415

        agent = tmp_path / "rules.md"
        agent.write_text("# Agent Rules\n\nbody\n", encoding="utf-8")
        spec = EvalSpec(
            name="framed",
            scenario="framed",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
        )
        captured = self._run(spec, workspace=tmp_path)
        system_prompt = captured["system_prompt_text"]
        assert system_prompt.endswith(LIVE_ENV_FRAMING)
        assert "issuing the actual tool call" in system_prompt
        assert "never print the command as text" in system_prompt


class TestSdkInProcessRunnerMessageMapping:
    def test_unknown_block_and_non_content_message_are_handled(self, tmp_path: Path) -> None:
        from claude_agent_sdk import SystemMessage, ThinkingBlock  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            SystemMessage(subtype="init", data={}),
            AssistantMessage(
                content=[
                    ThinkingBlock(thinking="hmm", signature="sig"),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}),
                ],
                model="haiku",
            ),
            _result(),
        ]
        query, _ = _fake_query(messages)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner(workspace=tmp_path).run(spec)
        # The thinking block is dropped, the tool_use is captured, the system message ignored.
        assert [c.name for c in run.tool_calls] == ["Bash"]
        assert run.text_blocks == ()

    def test_relative_agent_path_resolves_against_teatree_root(self, tmp_path: Path, monkeypatch) -> None:
        # cwd is a temp dir (first candidate misses), so resolution falls through
        # to the teatree-root candidate (exercises the continue + found branch).
        from teatree.eval.sdk_runner import _teatree_root  # noqa: PLC0415

        monkeypatch.chdir(tmp_path)
        rel = "skills/code/SKILL.md"
        assert (_teatree_root() / rel).is_file(), "test assumes the SKILL.md exists at the teatree root"
        spec = EvalSpec(
            name="rel",
            scenario="x",
            agent_path=rel,
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert captured["system_prompt_text"].strip()

    def test_relative_agent_path_not_found_raises(self, tmp_path: Path, monkeypatch) -> None:
        # No candidate resolves (cwd and teatree-root both miss) -> the loop
        # exhausts and the missing-file raise fires.
        monkeypatch.chdir(tmp_path)
        spec = EvalSpec(
            name="missing_rel",
            scenario="x",
            agent_path="no/such/relative/SKILL.md",
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(FileNotFoundError),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)


def _config(tmp_path: Path, *, max_budget_usd: float) -> CleanRoomConfig:
    return CleanRoomConfig(
        system_prompt="sp",
        workspace=tmp_path,
        cwd=str(tmp_path),
        env={},
        allowed_tools=("Bash",),
        model="haiku",
        max_turns=3,
        max_budget_usd=max_budget_usd,
    )


class TestBuildSdkOptionsBudget:
    def test_default_budget_is_the_cheap_lane_constant(self, tmp_path: Path) -> None:
        config = _config(tmp_path, max_budget_usd=float(MAX_BUDGET_USD))
        assert build_sdk_options(config).max_budget_usd == pytest.approx(float(MAX_BUDGET_USD))

    def test_non_default_budget_carries_into_options(self, tmp_path: Path) -> None:
        # build_sdk_options must read the budget from the config, NOT the constant —
        # this is the seam the benchmark's generous cap threads through.
        config = _config(tmp_path, max_budget_usd=2.5)
        assert build_sdk_options(config).max_budget_usd == pytest.approx(2.5)


class TestCalibratedCaps:
    """The metered lane's resource caps default GENEROUS, not the cheap-lane floor.

    A truncated run measures the cap, not behaviour — so the watchdog and the
    default per-scenario turn budget are raised generously (the first full
    metered run lost ~18 scenarios to cap truncation, a false negative). Each
    stays scenario-overridable (a scenario may still declare its own
    ``max_turns``) and env-configurable (default generous).
    """

    def test_watchdog_default_is_generous(self) -> None:
        # 120s was too tight for sub-agent-spawning scenarios (they timed out).
        # The default watchdog is raised to a generous value (was 120).
        assert DEFAULT_WATCHDOG_SECONDS >= 300
        assert WATCHDOG_SECONDS >= 300

    def test_default_max_turns_is_generous(self) -> None:
        # The old default of 4 force-FAILed multi-step / delegating scenarios.
        # A scenario needing N>old-default turns is no longer truncated by the
        # default — the default is generous (was 4).
        assert DEFAULT_MAX_TURNS >= 20
        assert EvalSpec.max_turns >= 20

    def test_a_scenario_needing_many_turns_is_not_force_failed_by_the_default(self, tmp_path: Path) -> None:
        # The lane default (no scenario-declared max_turns, no override) is the
        # generous DEFAULT_MAX_TURNS, so a many-step scenario gets room to finish.
        agent = tmp_path / "agent.md"
        agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
        spec = EvalSpec(
            name="multi_step",
            scenario="a delegating scenario needs many turns",
            agent_path=str(agent),
            prompt="x",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
        )
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert captured["options"].max_turns == DEFAULT_MAX_TURNS

    def test_clean_room_below_floor_turn_budget_is_lifted_to_the_floor(self, tmp_path: Path) -> None:
        # A clean-room scenario's tight max_turns is RAISED to CLEAN_ROOM_MIN_TURNS:
        # a correct early action must not be nullified by the #2192 cap-FAIL when the
        # model orients before/after acting. The floor never lowers a higher value.
        from teatree.eval.models import CLEAN_ROOM_MIN_TURNS  # noqa: PLC0415

        spec = _spec(tmp_path, max_turns=3, lane="clean_room")
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert captured["options"].max_turns == CLEAN_ROOM_MIN_TURNS

    def test_under_load_below_floor_turn_budget_is_kept_unchanged(self, tmp_path: Path) -> None:
        # The floor is clean-room ONLY — the under_load lane keeps its own
        # turn/watchdog calibration, so a low declared budget is used verbatim there.
        spec = _spec(tmp_path, max_turns=3, lane="under_load")
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert captured["options"].max_turns == 3

    def test_explicit_override_wins_over_the_clean_room_floor(self, tmp_path: Path) -> None:
        # An explicit --max-turns / T3_EVAL_MAX_TURNS override is honoured verbatim,
        # even below the floor — the operator asked for exactly that budget.
        spec = _spec(tmp_path, max_turns=3, lane="clean_room")
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=tmp_path, max_turns_override=2).run(spec)
        assert captured["options"].max_turns == 2


class TestCapsAreEnvConfigurable:
    """Each generous default is overridable via a ``T3_EVAL_*`` env var."""

    def test_watchdog_resolves_the_env_override(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_watchdog_seconds  # noqa: PLC0415

        monkeypatch.setenv("T3_EVAL_WATCHDOG_SECONDS", "450")
        assert resolve_watchdog_seconds() == pytest.approx(450.0)

    def test_watchdog_falls_back_to_the_generous_default(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_watchdog_seconds  # noqa: PLC0415

        monkeypatch.delenv("T3_EVAL_WATCHDOG_SECONDS", raising=False)
        assert resolve_watchdog_seconds() == pytest.approx(float(DEFAULT_WATCHDOG_SECONDS))

    def test_max_turns_override_resolves_the_env_value(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_max_turns_override  # noqa: PLC0415

        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "50")
        assert resolve_max_turns_override() == 50

    def test_max_turns_override_defers_to_per_scenario_when_unset(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_max_turns_override  # noqa: PLC0415

        monkeypatch.delenv("T3_EVAL_MAX_TURNS", raising=False)
        assert resolve_max_turns_override() is None

    def test_max_turns_override_ignores_a_non_positive_value(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_max_turns_override  # noqa: PLC0415

        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "0")
        assert resolve_max_turns_override() is None

    def test_max_turns_override_prefers_an_explicit_value_over_the_env(self, monkeypatch) -> None:
        from teatree.eval.sdk_runner import resolve_max_turns_override  # noqa: PLC0415

        monkeypatch.setenv("T3_EVAL_MAX_TURNS", "9")
        assert resolve_max_turns_override(explicit=4) == 4


def _budget_raising_query(message: str):
    """A fake ``query`` that raises a bare Exception(message) like the SDK's budget path."""

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        raise Exception(message)  # noqa: TRY002 — the SDK raises a BARE Exception for the budget breaker; this fake must reproduce that exact class so the runner's message-based catch is exercised.
        yield  # pragma: no cover

    return _query


class TestSdkInProcessRunnerBudgetExceeded:
    def _run_with_raising_query(self, spec: EvalSpec, query, **kwargs: Any):
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            return SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)

    def test_budget_exceeded_is_a_recorded_run_not_a_crash(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        assert run.is_error is True
        assert run.terminal_reason == "budget_exceeded"
        assert run.tool_calls == ()

    def test_budget_exceeded_recovers_the_partial_cost_from_the_message(self, tmp_path: Path) -> None:
        # The cap floor is recoverable from the "($0.1)" the SDK message carries,
        # so an over-budget cell renders a real cost, not a blank.
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        assert run.cost_usd == pytest.approx(0.1)

    def test_budget_exceeded_falls_back_to_cap_when_message_carries_no_amount(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget")
        run = self._run_with_raising_query(spec, query, max_budget_usd=2.0)
        assert run.cost_usd == pytest.approx(2.0)

    def test_typed_budget_exceeded_error_is_caught(self, tmp_path: Path) -> None:
        # A directly-raised BudgetExceededError (the typed alias) is also a run.
        spec = _spec(tmp_path)

        async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            message = "Reached maximum budget ($0.1)"
            raise BudgetExceededError(message)
            yield  # pragma: no cover

        run = self._run_with_raising_query(spec, _query)
        assert run.terminal_reason == "budget_exceeded"

    def test_non_budget_exception_still_propagates(self, tmp_path: Path) -> None:
        # Anti-vacuity: the catch is defensive, NOT a blanket swallow. An unrelated
        # error must surface (a swallowed one would hide a real crash).
        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: error_during_execution")
        with pytest.raises(Exception, match="error_during_execution"):
            self._run_with_raising_query(spec, query)

    def test_budget_exceeded_run_grades_to_a_fail_with_visible_cost(self, tmp_path: Path) -> None:
        # An over-budget cell is a real measurement: it grades to FAIL (not skip),
        # carrying the cap cost so the benchmark renders it legibly, not blank.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        query = _budget_raising_query("Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_raising_query(spec, query)
        result = evaluate(spec, run)
        assert result.skipped is False
        assert result.passed is False
        assert result.verdict == "fail"
        assert result.run.cost_usd == pytest.approx(0.1)


def _yield_then_raise_query(messages: list[Any], message: str):
    """A fake ``query`` that yields *messages*, THEN raises a bare ``Exception``.

    Models the SDK's real terminal-result shape (``query.py`` ``receive_messages``
    L852): every message gathered before the cap reaches the consumer's
    ``async for`` loop, then the trailing error sentinel raises a bare
    ``Exception``. The runner must keep the partial trajectory, not discard it.
    """

    async def _query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(0)
        for item in messages:
            yield item
        raise Exception(message)  # noqa: TRY002 — the SDK raises a BARE Exception mid-stream for any error-result subtype (budget, max-turns); this fake must reproduce that exact class so the runner's message-based classifier is exercised.

    return _query


class TestClassifyTerminalError:
    def test_classifies_the_sdk_max_budget_message(self) -> None:
        msg = "Claude Code returned an error result: Reached maximum budget ($0.1)"
        assert classify_terminal_error(msg) == "budget_exceeded"

    def test_classifies_the_sdk_max_turns_message(self) -> None:
        # The metered second-crash string: error_max_turns surfaces as
        # "Reached maximum number of turns (3)".
        msg = "Claude Code returned an error result: Reached maximum number of turns (3)"
        assert classify_terminal_error(msg) == "max_turns"

    def test_returns_none_for_a_genuine_error(self) -> None:
        assert classify_terminal_error("Claude Code returned an error result: error_during_execution") is None
        assert classify_terminal_error("some other RuntimeError about a socket") is None


#: A whole-skill system prompt is hundreds of KB; the metered lane crashed with
#: ``[Errno 7] Argument list too long`` (E2BIG) because the SDK rendered it as a
#: single ``--system-prompt <text>`` argv token. 200 KB reproduces that scale.
_HUGE_SYSTEM_PROMPT = "# Big Skill\n\n" + ("x" * 200_000)
#: A single argv token this large is what blew ARG_MAX. The fixed transport must
#: keep every token well under it (the prompt now travels as a file path).
_ARGV_TOKEN_CEILING = 8_192


class TestLargeSystemPromptDoesNotBlowArgMax:
    """A 200 KB system prompt must not become a giant argv token (E2BIG regression).

    The metered ``eval`` job failed before any scenario with
    ``CLIConnectionError: Failed to start Claude Code: [Errno 7] Argument list
    too long`` because the clean-room options carried the whole skill as a
    plain-string ``system_prompt``, which the SDK transport renders as
    ``--system-prompt <whole-skill>`` — one argv argument over the OS limit. The
    fix spills the prompt to a file and passes ``--system-prompt-file <path>``, so
    no single argv token grows with skill size.
    """

    def test_build_sdk_options_spills_a_huge_prompt_to_a_file(self, tmp_path: Path) -> None:
        config = CleanRoomConfig(
            system_prompt=_HUGE_SYSTEM_PROMPT,
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        options = build_sdk_options(config)
        # The prompt is a file reference, not an inline string the transport would
        # pass via argv.
        assert isinstance(options.system_prompt, dict)
        assert options.system_prompt["type"] == "file"
        assert resolve_system_prompt(options.system_prompt) == _HUGE_SYSTEM_PROMPT

    def test_sdk_command_has_no_arg_over_the_ceiling_for_a_huge_prompt(self, tmp_path: Path) -> None:
        # Exercise the REAL SDK transport arg builder: with the old inline-string
        # prompt this produced a single 200 KB argv token (E2BIG); the file-based
        # transport keeps every token small.
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport  # noqa: PLC0415

        config = CleanRoomConfig(
            system_prompt=_HUGE_SYSTEM_PROMPT,
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        options = build_sdk_options(config)
        transport = SubprocessCLITransport(prompt="hi", options=options)
        transport._cli_path = "/usr/local/bin/claude"
        command = transport._build_command()
        # No single argv token carries the prompt body, and the prompt text itself
        # appears in NO argv argument — it travels only inside the spilled file.
        assert all(len(arg) < _ARGV_TOKEN_CEILING for arg in command)
        assert not any(_HUGE_SYSTEM_PROMPT in arg for arg in command)
        # It is passed by reference, not value.
        assert "--system-prompt-file" in command

    def test_spill_round_trips_content_and_keeps_the_path_short(self, tmp_path: Path) -> None:
        ref = spill_system_prompt(_HUGE_SYSTEM_PROMPT, str(tmp_path))
        assert ref["type"] == "file"
        assert len(ref["path"]) < _ARGV_TOKEN_CEILING
        assert resolve_system_prompt(ref) == _HUGE_SYSTEM_PROMPT

    def test_runner_starts_a_huge_prompt_scenario_without_oserror(self, tmp_path: Path) -> None:
        # End-to-end at the runner boundary: a scenario whose skill is 200 KB runs
        # through the runner (SDK mocked) and produces a normal EvalRun — no
        # OSError/E2BIG at the spawn boundary.
        agent = tmp_path / "huge_skill.md"
        agent.write_text(_HUGE_SYSTEM_PROMPT, encoding="utf-8")
        spec = EvalSpec(
            name="huge",
            scenario="a 200KB skill must not blow ARG_MAX at spawn",
            agent_path=str(agent),
            prompt="do the one thing.",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
            tools=("Bash",),
        )
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner(workspace=tmp_path).run(spec)
        assert run.terminal_reason == "success"
        assert run.is_error is False
        assert _HUGE_SYSTEM_PROMPT in captured["system_prompt_text"]


class TestSuccessResultIsNotAnError:
    """A ``result`` mislabeled ``error_result: success`` must grade as a success.

    The metered run also raised ``Exception: Claude Code returned an error result:
    success`` at the end: the CLI exits non-zero while its ``result`` event subtype
    reads ``"success"``, and the SDK wraps that as a bare error Exception. The
    runner must recognize this SUCCESS terminus and grade the captured trajectory
    normally instead of crashing the whole run.
    """

    def _run_with_query(self, spec: EvalSpec, query, **kwargs: Any):
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            return SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)

    def test_is_success_result_error_recognizes_the_marker(self) -> None:
        assert is_success_result_error("Claude Code returned an error result: success") is True
        assert is_success_result_error("Claude Code returned an error result: error_max_turns") is False

    def test_success_labeled_error_after_a_result_grades_as_a_normal_run(self, tmp_path: Path) -> None:
        # The production case: the CLI exits non-zero on a ``"success"`` subtype, so
        # the captured ``result`` event carries BOTH ``subtype="success"`` AND
        # ``is_error=True``. The run must still grade from the captured trajectory as
        # a success — ``is_error`` cleared — not be forced to FAIL on the stray flag.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
            _result(subtype="success", is_error=True, total_cost_usd=0.0321),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: success")
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "success"
        assert run.is_error is False
        assert len(run.tool_calls) == 1
        assert run.tool_calls[0].input["command"].startswith("git worktree add")
        assert run.cost_usd == pytest.approx(0.0321)

    def test_success_labeled_run_grades_through_report_evaluate(self, tmp_path: Path) -> None:
        # The deliverable shape: a success-labeled run whose captured result carries
        # the stray ``is_error=True`` still produces a normal graded ScenarioResult
        # (not a forced FAIL), with the matcher deciding pass/fail.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
            _result(subtype="success", is_error=True, total_cost_usd=0.01),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: success")
        run = self._run_with_query(spec, query)
        result = evaluate(spec, run)
        assert result.skipped is False
        assert result.passed is True
        assert result.verdict == "pass"

    def test_a_genuine_error_result_still_propagates(self, tmp_path: Path) -> None:
        # Anti-vacuity: only the "success" subtype is rescued. A real error subtype
        # is NOT swallowed by the success path — it stays a propagating crash.
        spec = _spec(tmp_path)
        query = _yield_then_raise_query([], "Claude Code returned an error result: error_during_execution")
        with pytest.raises(Exception, match="error_during_execution"):
            self._run_with_query(spec, query)


class TestSdkInProcessRunnerMaxTurnsCapturesTrajectory:
    def _run_with_query(self, spec: EvalSpec, query, **kwargs: Any):
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            return SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)

    def test_max_turns_cap_keeps_the_tool_call_emitted_before_the_cap(self, tmp_path: Path) -> None:
        # The agent DID the expected tool call before turn 3, then hit the cap.
        # The partial trajectory must survive the bare Exception — not be discarded.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[
                    TextBlock(text="Creating a worktree first."),
                    ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"}),
                ],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "max_turns"
        assert len(run.tool_calls) == 1
        assert run.tool_calls[0].name == "Bash"
        assert run.tool_calls[0].input["command"].startswith("git worktree add")
        assert run.text_blocks == ("Creating a worktree first.",)

    def test_max_turns_cap_with_satisfying_trajectory_is_diagnostic_not_a_gate_pass(self, tmp_path: Path) -> None:
        # A capped run that captured a satisfying trajectory keeps that grading as
        # DIAGNOSTIC — the matcher still records a pass, ``is_error`` stays False,
        # and the cap is surfaced via ``terminal_reason``. But the run did NOT
        # finish, so it must NOT count as a GATE pass (#2192): a run that emitted
        # the expected early behavior yet hit a cap fails the gate, otherwise
        # raising the caps (#19) would mask real failures.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.is_error is False
        result = evaluate(spec, run)
        assert result.skipped is False
        # Diagnostic preserved: the matcher that matched on the partial trajectory
        # is still recorded as passed (the reason/why stays visible)...
        assert all(m.passed for m in result.matcher_results)
        # ...but the cap-truncated run is NOT a gate pass.
        assert result.passed is False
        assert result.verdict == "fail"

    def test_max_turns_cap_with_no_satisfying_call_grades_to_fail(self, tmp_path: Path) -> None:
        # The matchers still decide: a capped trajectory that does NOT satisfy the
        # positive matcher grades to FAIL — not a vacuous PASS.
        from teatree.eval.report import evaluate  # noqa: PLC0415

        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "echo no worktree here"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        result = evaluate(spec, run)
        assert result.verdict == "fail"

    def test_max_turns_cap_with_no_captured_messages_falls_back_to_terminal_shape(self, tmp_path: Path) -> None:
        # When nothing was captured before the cap, fall back to the empty
        # terminal shape: is_error=True, terminal_reason=max_turns, no tool calls.
        spec = _spec(tmp_path)
        query = _yield_then_raise_query([], "Claude Code returned an error result: Reached maximum number of turns (3)")
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "max_turns"
        assert run.is_error is True
        assert run.tool_calls == ()

    def test_max_turns_cap_recovers_cost_from_captured_result_message(self, tmp_path: Path) -> None:
        # max-turns carries no "($X)" in the message; cost comes from a captured
        # ResultMessage if the SDK emitted one before the cap.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
            _result(total_cost_usd=0.0789),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.cost_usd == pytest.approx(0.0789)

    def test_max_turns_cap_without_result_message_reports_zero_cost(self, tmp_path: Path) -> None:
        # No captured ResultMessage and no amount in the max-turns message -> cost
        # 0.0, with terminal_reason making the incompleteness visible.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(
            messages, "Claude Code returned an error result: Reached maximum number of turns (3)"
        )
        run = self._run_with_query(spec, query)
        assert run.cost_usd == pytest.approx(0.0)

    def test_budget_cap_with_captured_trajectory_keeps_the_tool_call(self, tmp_path: Path) -> None:
        # Generalization regression: the budget path now ALSO captures a partial
        # trajectory when one was emitted before the cap (was discarded before).
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "git worktree add ../wt HEAD"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: Reached maximum budget ($0.1)")
        run = self._run_with_query(spec, query)
        assert run.terminal_reason == "budget_exceeded"
        assert len(run.tool_calls) == 1
        assert run.is_error is False
        # The budget amount in the message is still the recovered cost floor.
        assert run.cost_usd == pytest.approx(0.1)

    def test_non_terminal_error_after_partial_messages_still_propagates(self, tmp_path: Path) -> None:
        # Anti-vacuity for the partial path: a genuine error mid-stream (after some
        # captured messages) is NOT classified as terminal, so it re-raises — a
        # swallowed crash would grade a broken run as a real measurement.
        spec = _spec(tmp_path)
        messages = [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
                model="haiku",
            ),
        ]
        query = _yield_then_raise_query(messages, "Claude Code returned an error result: error_during_execution")
        with pytest.raises(Exception, match="error_during_execution"):
            self._run_with_query(spec, query)


def _spec_with(
    tmp_path: Path,
    *,
    tools: tuple[str, ...],
    matchers: tuple[ExpectItem, ...],
) -> EvalSpec:
    """An :class:`EvalSpec` carrying arbitrary declared *tools* and *matchers*.

    The agent file and prompt are inert — only ``tools`` and ``matchers`` drive
    the disallowed-set computation under test.
    """
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="toolset_restriction",
        scenario="a scenario's declared tools restrict the model's available toolset",
        agent_path=str(agent),
        prompt="do the one action.",
        matchers=matchers,
        source_path=tmp_path / "spec.yaml",
        tools=tools,
    )


class TestComputeDisallowedTools:
    """A scenario's ``tools:`` plus its matcher-referenced tools fix the toolset.

    Under ``bypassPermissions`` ``allowed_tools`` only AUTO-APPROVES — it does not
    remove a tool from the model's available set. The metered lane therefore
    computes a ``disallowed_tools`` complement so a scenario declaring
    ``tools: [Write]`` no longer sees Bash/Read and spirals into exploration that
    blows ``max_turns`` (a false fail). The complement must NEVER disallow a tool
    any matcher references — positive OR negative — or a negative assertion would
    pass vacuously, hiding the very misbehaviour it tests.
    """

    def test_declared_only_tool_disallows_the_other_builtins(self, tmp_path: Path) -> None:
        # tools=[Write] + a positive Write matcher: Write stays available, the
        # spiral tools (Bash, Read) are removed from the model's toolset.
        spec = _spec_with(
            tmp_path,
            tools=("Write",),
            matchers=(Matcher(kind="positive", tool="Write", arg_path="file_path", operator="contains", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Bash" in disallowed
        assert "Read" in disallowed
        assert "Write" not in disallowed

    def test_escape_and_punt_tools_are_disallowed_when_undeclared(self, tmp_path: Path) -> None:
        # Metered verification showed a tools=[Bash] scenario spiral via ToolSearch
        # (tool-hunting) and AskUserQuestion (punting) instead of issuing its one
        # Bash command. Both are in KNOWN_BUILTIN_TOOLS, so a scenario that neither
        # declares nor references them gets them removed from the model's toolset.
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "ToolSearch" in disallowed
        assert "AskUserQuestion" in disallowed

    def test_declared_askuserquestion_stays_available(self, tmp_path: Path) -> None:
        # comm_asks_via_askuserquestion_not_chat shape: a scenario that DECLARES
        # AskUserQuestion keeps it available — only undeclared/unreferenced punt
        # tools are removed. ToolSearch is never declared anywhere, so it stays
        # disallowed even here.
        spec = _spec_with(
            tmp_path,
            tools=("AskUserQuestion", "Bash"),
            matchers=(Matcher(kind="positive", tool="AskUserQuestion", arg_path="questions", operator="~", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "AskUserQuestion" not in disallowed
        assert "ToolSearch" in disallowed

    def test_negative_matcher_tool_is_never_disallowed(self, tmp_path: Path) -> None:
        # The orchestrator_delegates_test_writing shape: tools=[Bash, Edit, Task]
        # with a NEGATIVE Write matcher (the orchestrator must DELEGATE, not write
        # code itself). Write must stay AVAILABLE so the negative assertion is not
        # vacuous; the declared tools must not be disallowed either. The declared
        # spawn tool ``Task`` canonicalizes to the CLI's real ``Agent`` tool — so
        # ``Agent`` (not the phantom ``Task``) must NOT be disallowed: that is the
        # whole delegation fix (#2639). ``Task`` is no CLI built-in, so it can never
        # appear in the denylist regardless.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Edit", "Task"),
            matchers=(
                Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="test"),
                Matcher(kind="negative", tool="Write", arg_path="file_path", operator="~", value=r"test_.*\.py"),
            ),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Write" not in disallowed
        assert "Bash" not in disallowed
        assert "Edit" not in disallowed
        assert "Agent" not in disallowed

    def test_lowercase_declared_tool_is_canonicalized(self, tmp_path: Path) -> None:
        # A spec declaring the lowercase alias "bash" must NOT have Bash disallowed
        # — declared tools are canonicalized the SAME way the grader canonicalizes.
        spec = _spec_with(
            tmp_path,
            tools=("bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Bash" not in disallowed

    def test_any_of_alternative_tools_are_never_disallowed(self, tmp_path: Path) -> None:
        # Each AnyOf alternative is a positive matcher; its tool must stay available
        # so the disjunction can hold on either branch. A ``Task`` alternative
        # canonicalizes to the CLI's real ``Agent`` spawn tool, so ``Agent`` must
        # stay available (#2639) — not the phantom ``Task``.
        spec = _spec_with(
            tmp_path,
            tools=(),
            matchers=(
                AnyOf(
                    alternatives=(
                        Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),
                        Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),
                    )
                ),
            ),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Agent" not in disallowed
        assert "Bash" not in disallowed

    def test_final_state_matcher_contributes_no_tool(self, tmp_path: Path) -> None:
        # A FinalStateMatcher has no tool, so it neither adds to nor removes from
        # the disallow set — the declared tools alone govern.
        spec = _spec_with(
            tmp_path,
            tools=("Read",),
            matchers=(FinalStateMatcher(operator="contains", value="done"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Read" not in disallowed
        assert "Bash" in disallowed

    def test_skill_is_never_disallowed(self, tmp_path: Path) -> None:
        # "Skill" is deliberately absent from KNOWN_BUILTIN_TOOLS — it is left
        # untouched so a scenario can always load a skill.
        assert "Skill" not in KNOWN_BUILTIN_TOOLS
        spec = _spec_with(
            tmp_path,
            tools=("Write",),
            matchers=(Matcher(kind="positive", tool="Write", arg_path="file_path", operator="contains", value="x"),),
        )
        assert "Skill" not in compute_disallowed_tools(spec)

    def test_disallowed_set_is_sorted_and_deterministic(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Write",),
            matchers=(Matcher(kind="positive", tool="Write", arg_path="file_path", operator="contains", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert list(disallowed) == sorted(disallowed)

    def test_known_builtin_tools_is_the_complete_bundled_cli_set(self) -> None:
        # The denylist is exhaustive only if KNOWN_BUILTIN_TOOLS is the COMPLETE
        # bundled-CLI built-in set. An incomplete set is exactly why PushNotification
        # leaked. Assert the escape/spiral tools the metered runs surfaced are all
        # present (plus the full set excludes nothing the model can reach). The three
        # Agent-Team tools (SendMessage, TaskCreate, TaskUpdate) are real CLI built-ins
        # the team-mode runtime grants — confirmed against the binary — so they belong
        # in the complete set the team scenarios (delegate-vs-spawn) declare. MultiEdit
        # was REMOVED from the CLI registry (current bundled CLI 2.1.x); it must NOT be
        # present or every clean-room invocation prints "matches no known tool".
        expected = {
            "Agent",
            "AskUserQuestion",
            "Bash",
            "BashOutput",
            "Edit",
            "EnterPlanMode",
            "ExitPlanMode",
            "Glob",
            "Grep",
            "KillBash",
            "KillShell",
            "ListMcpResources",
            "Monitor",
            "NotebookEdit",
            "PushNotification",
            "Read",
            "ReadMcpResource",
            "SendMessage",
            "Task",
            "TaskCreate",
            "TaskUpdate",
            "TodoWrite",
            "ToolSearch",
            "WebFetch",
            "WebSearch",
            "Write",
        }
        assert set(KNOWN_BUILTIN_TOOLS) == expected
        assert len(KNOWN_BUILTIN_TOOLS) == 26
        assert "MultiEdit" not in KNOWN_BUILTIN_TOOLS
        for escape_tool in ("PushNotification", "ToolSearch", "AskUserQuestion"):
            assert escape_tool in KNOWN_BUILTIN_TOOLS
        for team_tool in ("SendMessage", "TaskCreate", "TaskUpdate"):
            assert team_tool in KNOWN_BUILTIN_TOOLS

    def test_monitor_is_a_builtin_disallowed_for_a_non_monitor_scenario(self, tmp_path: Path) -> None:
        # Monitor IS a built-in now (background scenarios declare it), so a scenario
        # that neither declares nor references it gets it disallowed — a non-background
        # scenario should not be able to spiral into Monitor.
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert "Monitor" in disallowed

    def test_monitor_stays_available_for_a_background_scenario(self, tmp_path: Path) -> None:
        # A background-shape spec declaring Monitor keeps it available: NOT in the
        # disallowed complement, and IN the allowlist of available tools.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task", "Monitor"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="ci"),),
        )
        assert "Monitor" not in compute_disallowed_tools(spec)
        assert "Monitor" in compute_available_tools(spec)


class TestComputeAvailableTools:
    """The ALLOWLIST: only a scenario's declared + matcher-referenced tools.

    The PRIMARY restriction (the SDK's ``--tools`` allowlist). The model sees
    only the listed tools, regardless of permission mode — the robust fix for the
    fragile denylist (which leaked any built-in not yet enumerated). Available =
    canonicalize(declared) union matcher-referenced.
    """

    def test_declared_and_referenced_only(self, tmp_path: Path) -> None:
        # tools=[Bash] + a Bash matcher → exactly ("Bash",): no PushNotification,
        # ToolSearch, Monitor, or any other built-in leaks in.
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        )
        assert compute_available_tools(spec) == ("Bash",)

    def test_negative_matcher_tool_is_available_so_assertion_is_not_vacuous(self, tmp_path: Path) -> None:
        # orchestrator_delegates_test_writing shape: tools=[Bash, Edit, Task] with a
        # NEGATIVE Write matcher. Write must be AVAILABLE so the model CAN call it —
        # otherwise the no_tool_call assertion passes vacuously. The declared spawn
        # tool ``Task`` canonicalizes to the CLI's real ``Agent`` tool (#2639), so the
        # allowlist exposes ``Agent`` — the model can actually delegate — never the
        # phantom ``Task`` the bundled CLI does not register.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Edit", "Task"),
            matchers=(
                Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="test"),
                Matcher(kind="negative", tool="Write", arg_path="file_path", operator="~", value=r"test_.*\.py"),
            ),
        )
        available = compute_available_tools(spec)
        assert set(available) == {"Agent", "Bash", "Edit", "Write"}

    def test_lowercase_declared_tool_is_canonicalized(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        )
        assert compute_available_tools(spec) == ("Bash",)

    def test_available_is_sorted_and_deterministic(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Task", "Bash", "Edit"),
            matchers=(Matcher(kind="negative", tool="Write", arg_path="file_path", operator="~", value="x"),),
        )
        available = compute_available_tools(spec)
        assert list(available) == sorted(available)

    def test_no_declared_no_referenced_is_empty(self, tmp_path: Path) -> None:
        # A scenario that declares no tools and references none → empty allowlist.
        # The edge case build_sdk_options must render as `tools=None` (CLI default),
        # NOT an empty `--tools ""`.
        spec = _spec_with(
            tmp_path,
            tools=(),
            matchers=(FinalStateMatcher(operator="contains", value="done"),),
        )
        assert compute_available_tools(spec) == ()


class TestDisallowedToolsFlowToOptions:
    """The computed disallowed set reaches ``ClaudeAgentOptions.disallowed_tools``."""

    def _run(self, spec: EvalSpec, **kwargs: Any):
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)
        return captured

    def test_build_sdk_options_forwards_disallowed_tools(self, tmp_path: Path) -> None:
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Write",),
            model="haiku",
            max_turns=3,
            disallowed_tools=("Bash", "Read"),
        )
        options = build_sdk_options(config)
        assert list(options.disallowed_tools) == ["Bash", "Read"]

    def test_default_disallowed_tools_is_empty_for_the_judge_path(self, tmp_path: Path) -> None:
        # CleanRoomConfig defaults disallowed_tools to () so the judge path (which
        # shares build_sdk_options) is unchanged.
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        assert config.disallowed_tools == ()
        assert list(build_sdk_options(config).disallowed_tools) == []

    def test_runner_computes_and_forwards_disallowed_tools(self, tmp_path: Path) -> None:
        # End-to-end: a tools=[Write] scenario reaches the SDK options with the
        # spiral builtins (Bash, Read) disallowed and Write still available.
        spec = _spec_with(
            tmp_path,
            tools=("Write",),
            matchers=(Matcher(kind="positive", tool="Write", arg_path="file_path", operator="contains", value="x"),),
        )
        captured = self._run(spec)
        disallowed = list(captured["options"].disallowed_tools)
        assert "Bash" in disallowed
        assert "Read" in disallowed
        assert "Write" not in disallowed

    def test_build_sdk_options_sets_tools_allowlist(self, tmp_path: Path) -> None:
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            available_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        options = build_sdk_options(config)
        assert options.tools == ["Bash"]

    def test_build_sdk_options_empty_available_tools_renders_none_not_empty_list(self, tmp_path: Path) -> None:
        # The CLI renders `tools=[]` as `--tools ""` (NO tools) but `tools=None` as
        # the CLI default. A scenario that didn't opt into an allowlist must get the
        # default, never an accidental no-tools run.
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=(),
            available_tools=(),
            model="haiku",
            max_turns=3,
        )
        options = build_sdk_options(config)
        assert options.tools is None

    def test_default_available_tools_is_empty_so_judge_path_uses_cli_default(self, tmp_path: Path) -> None:
        # CleanRoomConfig defaults available_tools to () so the judge path (which
        # shares build_sdk_options) gets tools=None — the CLI default toolset.
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        assert config.available_tools == ()
        assert build_sdk_options(config).tools is None

    def test_runner_computes_and_forwards_available_tools_allowlist(self, tmp_path: Path) -> None:
        # End-to-end: a tools=[Write] scenario reaches the SDK options with the
        # tools allowlist set to exactly ["Write"] — the model sees only Write.
        spec = _spec_with(
            tmp_path,
            tools=("Write",),
            matchers=(Matcher(kind="positive", tool="Write", arg_path="file_path", operator="contains", value="x"),),
        )
        captured = self._run(spec)
        assert captured["options"].tools == ["Write"]

    def test_runner_forwards_write_in_allowlist_for_orchestrator_shape(self, tmp_path: Path) -> None:
        # The negative-Write orchestrator shape: options.tools must contain Write so
        # the model CAN write (and the negative assertion is not vacuous).
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Edit", "Task"),
            matchers=(
                Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="test"),
                Matcher(kind="negative", tool="Write", arg_path="file_path", operator="~", value=r"test_.*\.py"),
            ),
        )
        captured = self._run(spec)
        assert "Write" in captured["options"].tools


class TestTaskAliasesToAgentSpawnTool:
    """``Task`` (the scenario/UI name) canonicalizes to the CLI's real ``Agent`` tool (#2639).

    The bundled ``claude`` CLI registers the sub-agent SPAWN tool as ``Agent`` — it
    has NO ``Task`` tool (``Task`` resolves to no known tool and is silently
    dropped, the same toolset-drift class as the removed ``MultiEdit`` in #2627).
    Before the fix, a scenario declaring ``tools: [..., Task]`` produced a ``--tools``
    allowlist with the phantom ``Task`` AND pushed the REAL ``Agent`` onto the
    ``--disallowedTools`` denylist, so the orchestrator-delegation scenarios could
    never call a spawn tool and their ``tool_call: Task`` matchers could never match
    the emitted ``Agent`` call.
    """

    def test_canonicalize_maps_task_to_agent(self) -> None:
        # The single normalization both the grader and the toolset restriction apply:
        # a matcher's `tool: Task` and a declared `tools: [Task]` both resolve to the
        # CLI's real `Agent` spawn tool — so the matcher's expected name and the
        # emitted call name agree.
        assert canonicalize_tool("Task") == "Agent"
        assert canonicalize_tool("task") == "Agent"

    def test_team_task_list_builtins_are_not_aliased(self) -> None:
        # The exact-key lowercase lookup must leave the DISTINCT team-mode task-list
        # built-ins untouched — they are real, separate CLI tools, not the spawn tool.
        for name in ("TaskCreate", "TaskUpdate", "TaskList", "TaskGet"):
            assert canonicalize_tool(name) == name

    def test_declared_task_exposes_agent_in_the_allowlist(self, tmp_path: Path) -> None:
        # tools=[Bash, Task]: the allowlist exposes Agent (the real spawn tool), never
        # the phantom Task — so the model can actually delegate.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        available = compute_available_tools(spec)
        assert "Agent" in available
        assert "Task" not in available

    def test_declared_task_does_not_disallow_agent(self, tmp_path: Path) -> None:
        # The regression: before the fix Agent (the only usable spawn tool) was on
        # the denylist for a Task-declaring scenario, so delegation was impossible.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        assert "Agent" not in compute_disallowed_tools(spec)

    def test_matcher_only_task_reference_exposes_agent(self, tmp_path: Path) -> None:
        # A scenario need not DECLARE Task — a matcher that references Task is enough
        # to land Agent in the available set, so the negative/positive assertion can
        # observe the real spawn call.
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        assert "Agent" in compute_available_tools(spec)
        assert "Agent" not in compute_disallowed_tools(spec)


class TestDelegationScenarioGetsEmptyDenylist:
    """A delegation scenario gets an EMPTY ``--disallowedTools`` so ``Agent`` survives.

    The bundled CLI disables the ``Agent`` SPAWN tool whenever ANY
    ``--disallowedTools`` denylist is present — even one that does NOT name
    ``Agent``. The ``--tools`` allowlist is the PRIMARY restriction and alone
    confines the toolset, so a delegation scenario drops the defense-in-depth
    denylist entirely; a non-delegation scenario keeps it. Without this, ``Agent``
    is allowlisted yet the model still reports having no spawn tool (#2639).
    """

    def test_delegation_scenario_has_empty_denylist(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        assert compute_disallowed_tools(spec) == ()

    def test_non_delegation_scenario_keeps_a_denylist(self, tmp_path: Path) -> None:
        # The denylist is unchanged for scenarios that do NOT reach the spawn tool:
        # the spiral tools are still removed so a tools=[Bash] scenario can't wander.
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),),
        )
        disallowed = compute_disallowed_tools(spec)
        assert disallowed != ()
        assert "ToolSearch" in disallowed
        assert "AskUserQuestion" in disallowed

    def test_runner_forwards_empty_denylist_for_delegation_scenario(self, tmp_path: Path) -> None:
        # End-to-end: a delegation scenario reaches the SDK options with Agent
        # allowlisted, an agents def, AND no disallowed_tools — the combination the
        # bundled CLI needs to actually expose the spawn tool to the model.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=spec.source_path.parent).run(spec)
        options = captured["options"]
        assert "Agent" in options.tools
        assert list(options.disallowed_tools) == []
        assert options.agents is not None
        assert DELEGATION_SUBAGENT_NAME in options.agents


class TestDelegationAgentsProvisioning:
    """A delegation scenario is given an ``agents`` definition so ``Agent`` is usable.

    The ``Agent`` tool is only genuinely usable when the SDK is handed sub-agent
    definitions over the initialize request — the SDK documents ``agents`` as the
    way to programmatically define custom sub-agents the ``Agent`` tool can spawn.
    The runner provisions a generic delegation sub-agent for exactly the scenarios
    whose toolset exposes the spawn tool, and leaves every other scenario's
    ``agents`` at ``None``.
    """

    def _run(self, spec: EvalSpec, **kwargs: Any):
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)
        return captured

    def test_spawn_tool_constant_is_agent(self) -> None:
        # Pins the one name the runner and toolset agree gates delegation.
        assert SUBAGENT_SPAWN_TOOL == "Agent"

    def test_delegation_scenario_exposes_spawn(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        assert scenario_exposes_subagent_spawn(spec) is True

    def test_non_delegation_scenario_does_not_expose_spawn(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),),
        )
        assert scenario_exposes_subagent_spawn(spec) is False

    def test_build_delegation_agents_for_delegation_scenario(self, tmp_path: Path) -> None:
        # A scenario exposing the spawn tool gets a single generic delegate sub-agent
        # keyed by DELEGATION_SUBAGENT_NAME, on the inherited model.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        agents = build_delegation_agents(spec)
        assert agents is not None
        assert set(agents) == {DELEGATION_SUBAGENT_NAME}
        assert agents[DELEGATION_SUBAGENT_NAME].model == "inherit"

    def test_build_delegation_agents_none_for_non_delegation_scenario(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),),
        )
        assert build_delegation_agents(spec) is None

    def test_build_sdk_options_defaults_agents_to_none_for_judge_path(self, tmp_path: Path) -> None:
        # CleanRoomConfig defaults agents to None so the judge / non-delegation path
        # (which shares build_sdk_options) is unchanged — no sub-agents.
        config = CleanRoomConfig(
            system_prompt="sp",
            workspace=tmp_path,
            cwd=str(tmp_path),
            env={},
            allowed_tools=("Bash",),
            model="haiku",
            max_turns=3,
        )
        assert config.agents is None
        assert build_sdk_options(config).agents is None

    def test_runner_forwards_agents_for_a_delegation_scenario(self, tmp_path: Path) -> None:
        # End-to-end: a Task-declaring scenario reaches the SDK options with both the
        # Agent tool allowlisted AND an agents definition, so the model can delegate.
        spec = _spec_with(
            tmp_path,
            tools=("Bash", "Task"),
            matchers=(Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="x"),),
        )
        captured = self._run(spec)
        options = captured["options"]
        assert "Agent" in options.tools
        assert options.agents is not None
        assert DELEGATION_SUBAGENT_NAME in options.agents

    def test_runner_forwards_no_agents_for_a_non_delegation_scenario(self, tmp_path: Path) -> None:
        spec = _spec_with(
            tmp_path,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),),
        )
        assert self._run(spec)["options"].agents is None


class TestSpawningScenarioRunsAgainstEphemeralCheckout:
    """A sub-agent-spawning scenario runs against a THROWAWAY isolated checkout.

    The spawned SDK sub-agent does real, destructive git work; without isolation
    it locates the developer's REAL clone (via the editable install + shared
    ``.git`` — a neutral cwd does NOT block that) and corrupts it. The fix points
    the SDK ``cwd`` + ``add_dirs`` + the resolution env at a per-run ephemeral
    ``git worktree --detach``. These tests stub the ephemeral provisioner — no real
    sub-agent runs — and assert the SDK options reach the throwaway, NOT the real
    clone, and that a NON-spawning scenario is unchanged.
    """

    def _run(self, spec: EvalSpec, **kwargs: Any):
        query, captured = _fake_query([_result()])
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            SdkInProcessRunner(workspace=spec.source_path.parent, **kwargs).run(spec)
        return captured

    def _spawning_spec(self, spec_dir: Path) -> EvalSpec:
        spec_dir.mkdir(parents=True, exist_ok=True)
        return _spec_with(
            spec_dir,
            tools=("Bash", "Agent"),
            matchers=(Matcher(kind="positive", tool="Agent", arg_path="prompt", operator="~", value="x"),),
        )

    def _non_spawning_spec(self, spec_dir: Path) -> EvalSpec:
        spec_dir.mkdir(parents=True, exist_ok=True)
        return _spec_with(
            spec_dir,
            tools=("Bash",),
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="~", value="x"),),
        )

    def test_spawning_scenario_points_cwd_and_add_dirs_at_the_throwaway(self, tmp_path: Path) -> None:
        ephemeral = tmp_path / "throwaway" / "teatree"
        ephemeral.mkdir(parents=True)
        real_workspace = tmp_path / "spec-dir"

        @contextmanager
        def _fake_provision():
            yield ephemeral

        spec = self._spawning_spec(real_workspace)
        with patch("teatree.eval.sdk_runner.provision_ephemeral_checkout", _fake_provision):
            captured = self._run(spec)

        options = captured["options"]
        assert options.cwd == str(ephemeral)
        assert options.add_dirs == [str(ephemeral)]
        # The SDK is NEVER pointed at the real spec/workspace dir for a spawning scenario.
        assert str(real_workspace) not in options.add_dirs
        assert options.cwd != str(real_workspace)

    def test_spawning_scenario_overlays_env_to_resolve_into_the_throwaway(self, tmp_path: Path) -> None:
        ephemeral = tmp_path / "throwaway" / "teatree"
        ephemeral.mkdir(parents=True)

        @contextmanager
        def _fake_provision():
            yield ephemeral

        spec = self._spawning_spec(tmp_path / "spec-dir")
        with patch("teatree.eval.sdk_runner.provision_ephemeral_checkout", _fake_provision):
            captured = self._run(spec)

        env = captured["options"].env
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(ephemeral / "src")

    def test_non_spawning_scenario_never_provisions_an_ephemeral_checkout(self, tmp_path: Path) -> None:
        real_workspace = tmp_path / "spec-dir"
        spec = self._non_spawning_spec(real_workspace)
        called = {"n": 0}

        @contextmanager
        def _tracking_provision():
            called["n"] += 1
            yield tmp_path / "should-not-be-used"

        with patch("teatree.eval.sdk_runner.provision_ephemeral_checkout", _tracking_provision):
            captured = self._run(spec)

        assert called["n"] == 0, "a non-spawning scenario must NOT provision an ephemeral checkout"
        # The non-spawning path keeps the configured workspace as add_dirs.
        assert captured["options"].add_dirs == [str(real_workspace)]

    def test_ephemeral_checkout_is_cleaned_up_after_the_run(self, tmp_path: Path) -> None:
        # The provisioner's context-manager teardown runs after _drive returns.
        entered: dict[str, bool] = {"open": False}
        ephemeral = tmp_path / "throwaway" / "teatree"
        ephemeral.mkdir(parents=True)

        @contextmanager
        def _tracking_provision():
            entered["open"] = True
            try:
                yield ephemeral
            finally:
                entered["open"] = False

        spec = self._spawning_spec(tmp_path / "spec-dir")
        with patch("teatree.eval.sdk_runner.provision_ephemeral_checkout", _tracking_provision):
            self._run(spec)

        assert entered["open"] is False, "the ephemeral checkout context must be exited (cleaned up)"


class TestResolveClaudePath:
    """Re-resolve ``claude`` across a transient mid-run auto-update, bounded.

    ``shutil.which("claude")`` returns ``None`` for a moment while the bundled CLI
    auto-updates (the nvm symlink is swapped). A single miss must not red the rest
    of the batch, so the resolver re-probes with a bounded backoff — but a
    genuinely-absent binary still returns ``None`` after the bounded attempts,
    never an infinite loop.
    """

    def test_returns_path_on_first_success_without_sleeping(self) -> None:
        sleeps: list[float] = []
        with patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/local/bin/claude"):
            path = resolve_claude_path(sleep=sleeps.append)
        assert path == "/usr/local/bin/claude"
        assert sleeps == [], "a first-attempt hit must not sleep"

    def test_re_resolves_after_a_transient_miss(self) -> None:
        # None (mid-update) then a valid path: the resolver rides out the swap.
        sleeps: list[float] = []
        results = iter([None, None, "/usr/local/bin/claude"])
        with patch("teatree.eval.sdk_runner.shutil.which", side_effect=lambda _name: next(results)):
            path = resolve_claude_path(sleep=sleeps.append)
        assert path == "/usr/local/bin/claude"
        assert len(sleeps) == 2, "two transient misses should back off twice, then succeed"

    def test_returns_none_after_bounded_attempts_when_genuinely_absent(self) -> None:
        sleeps: list[float] = []
        with patch("teatree.eval.sdk_runner.shutil.which", return_value=None):
            path = resolve_claude_path(max_attempts=3, sleep=sleeps.append)
        assert path is None
        # Bounded: max_attempts probes, max_attempts-1 backoffs — never an infinite loop.
        assert len(sleeps) == 2

    def test_default_attempts_are_bounded(self) -> None:
        calls = {"n": 0}

        def _always_missing(_name: str) -> None:
            calls["n"] += 1

        with patch("teatree.eval.sdk_runner.shutil.which", side_effect=_always_missing):
            assert resolve_claude_path(sleep=lambda _s: None) is None
        assert calls["n"] == CLAUDE_RESOLVE_MAX_ATTEMPTS


class TestRunnerRetriesTransientMissingClaude:
    """The runner re-resolves ``claude`` rather than skipping on a transient miss."""

    def test_run_proceeds_after_a_transient_miss(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        query, _ = _fake_query([_result(total_cost_usd=0.01)])
        results = iter([None, "/usr/local/bin/claude", "/usr/local/bin/claude"])
        with (
            patch(
                "teatree.eval.sdk_runner.shutil.which", side_effect=lambda _name: next(results, "/usr/local/bin/claude")
            ),
            patch("teatree.eval.sdk_runner.time.sleep", lambda _s: None),
            patch("teatree.eval.sdk_runner.query", query),
        ):
            run = SdkInProcessRunner().run(spec)
        assert run.terminal_reason == "success", "a transient claude miss must not skip the scenario"
        assert run.is_error is False

    def test_require_executed_still_hard_errors_when_persistently_absent(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value=None),
            patch("teatree.eval.sdk_runner.time.sleep", lambda _s: None),
            pytest.raises(ClaudeCliMissingError),
        ):
            SdkInProcessRunner(require_executed=True).run(spec)
