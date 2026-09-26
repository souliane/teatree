"""The factory's ``PreCompact`` tripwire blocks every compaction and ends the run on an automatic one."""

import ast
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import HookJSONOutput, HookMatcher, PreCompactHookInput
from django.test import TestCase

import teatree
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness, with_compaction_off
from teatree.agents.one_shot import OneShotSpec, _clean_room_options
from teatree.eval.api_runner import CleanRoomConfig, build_sdk_options
from teatree.loop import ci_eval_heal_fixer
from teatree.loops.dream.sdk_distiller import _distill_options
from teatree.loops.dream.sdk_eval_synthesizer import _synth_options
from tests.teatree_agents._sdk_fake import FakeHarness, FakeHarnessSession


class _ExitedSession:
    async def interrupt(self) -> None:
        msg = "the CLI already exited"
        raise RuntimeError(msg)


def _pre_compact(trigger: str) -> PreCompactHookInput:
    return PreCompactHookInput(
        hook_event_name="PreCompact",
        trigger="auto" if trigger == "auto" else "manual",
        custom_instructions=None,
        session_id="s1",
        transcript_path="",
        cwd="",
    )


async def _fire(guard: CompactionGuard, trigger: str) -> HookJSONOutput:
    output = await guard.pre_compact(_pre_compact(trigger), None, {"signal": None})
    await asyncio.sleep(0)
    return output


def _armed(trigger: str) -> tuple[CompactionGuard, FakeHarnessSession, HookJSONOutput]:
    guard = CompactionGuard()
    session = FakeHarnessSession([])
    guard.arm(session.interrupt)
    return guard, session, asyncio.run(_fire(guard, trigger))


class TestAnAutomaticCompaction:
    def test_it_is_blocked_and_the_run_is_told_to_stop(self) -> None:
        guard, _, output = _armed("auto")

        assert output.get("decision") == "block"
        assert output.get("continue_") is False
        assert guard.stopped_run
        assert guard.triggers == ["auto"]

    def test_the_armed_session_is_interrupted(self) -> None:
        _, session, _ = _armed("auto")

        assert session.interrupted

    def test_an_unarmed_guard_still_blocks_it(self) -> None:
        guard = CompactionGuard()

        output = asyncio.run(_fire(guard, "auto"))

        assert output.get("decision") == "block"
        assert guard.stopped_run

    def test_an_interrupt_that_fails_is_reported_and_never_breaks_the_hook(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = CompactionGuard()
        guard.arm(_ExitedSession().interrupt)

        with caplog.at_level(logging.WARNING, logger="teatree.agents.compaction_guard"):
            output = asyncio.run(_fire(guard, "auto"))

        assert output.get("decision") == "block"
        assert "interrupt" in caplog.text


class TestAManualCompaction:
    def test_it_is_blocked_without_ending_the_run(self) -> None:
        guard, session, output = _armed("manual")

        assert output.get("decision") == "block"
        assert "continue_" not in output
        assert not session.interrupted
        assert not guard.stopped_run


class TestAGuardedHarness:
    def test_the_session_it_opens_is_the_one_an_automatic_compaction_interrupts(self) -> None:
        guard = CompactionGuard()
        guarded = GuardedHarness(harness=FakeHarness([]), guard=guard)

        async def _open_and_compact() -> bool:
            async with guarded.open(options=None) as session:
                await _fire(guard, "auto")
                return getattr(session, "interrupted", False)

        assert asyncio.run(_open_and_compact())

    def test_it_reports_the_wrapped_harness_capabilities(self) -> None:
        harness = FakeHarness([])

        assert GuardedHarness(harness=harness, guard=CompactionGuard()).capabilities is harness.capabilities


class TestWithCompactionOff:
    def test_it_switches_compaction_off_on_top_of_the_env_already_pinned(self) -> None:
        options = with_compaction_off(ClaudeAgentOptions(env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x"}))

        assert options.env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x", "DISABLE_COMPACT": "1"}

    def test_it_registers_the_given_guard_beside_the_hooks_already_armed(self) -> None:
        guard = CompactionGuard()
        stop_gate = HookMatcher(hooks=[])

        options = with_compaction_off(ClaudeAgentOptions(hooks={"Stop": [stop_gate]}), guard)

        hooks = options.hooks or {}
        assert hooks["Stop"] == [stop_gate]
        [matcher] = hooks["PreCompact"]
        assert matcher.hooks == [guard.pre_compact]


_SOURCE_ROOT = Path(teatree.__file__).parent
_HELPER = "with_compaction_off"
_OPENERS = frozenset({"ClaudeSDKClient", "query"})
_KNOWN_COMPOSERS = frozenset(
    {
        "agents/_runner_options.py::_build_options",
        "agents/one_shot.py::_clean_room_options",
        "cli/doctor/checks_agent_spawn.py::_check_agent_spawn_headroom",
        "eval/api_runner.py::build_sdk_options",
        "loop/ci_eval_heal_fixer.py::_fix_turn_options",
        "loops/dream/sdk_distiller.py::_distill_options",
        "loops/dream/sdk_eval_synthesizer.py::_synth_options",
    }
)


@dataclass(frozen=True)
class _Composer:
    location: str
    routed: bool


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _composers(source: str, relative: str) -> list[_Composer]:
    found: list[_Composer] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            called = {_called_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)}
            if "ClaudeAgentOptions" in called:
                found.append(_Composer(location=f"{relative}::{node.name}", routed=_HELPER in called))
    return found


def _openers_without_options(source: str, relative: str) -> list[str]:
    tree = ast.parse(source)
    sdk_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("claude_agent_sdk")
        for alias in node.names
        if alias.name in _OPENERS
    }
    return [
        f"{relative}:{call.lineno}"
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and _called_name(call) in sdk_names
        and not any(keyword.arg == "options" for keyword in call.keywords)
        and len(call.args) < (2 if _called_name(call) == "query" else 1)
    ]


def _source_tree() -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(_SOURCE_ROOT)), path.read_text(encoding="utf-8"))
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
    ]


class TestEveryHeadlessComposerRunsUncompacted:
    """Every factory ``ClaudeAgentOptions`` composer routes through the one switch-and-tripwire helper."""

    def test_the_scan_reads_the_tree_and_finds_every_known_composer(self) -> None:
        tree = _source_tree()
        found = {composer.location for relative, source in tree for composer in _composers(source, relative)}

        assert len(tree) > 100
        assert found >= _KNOWN_COMPOSERS

    def test_every_options_composer_switches_compaction_off(self) -> None:
        unrouted = [
            composer.location
            for relative, source in _source_tree()
            for composer in _composers(source, relative)
            if not composer.routed
        ]

        assert unrouted == []

    def test_every_sdk_session_is_opened_with_explicit_options(self) -> None:
        bare = [site for relative, source in _source_tree() for site in _openers_without_options(source, relative)]

        assert bare == []

    def test_a_composer_that_skips_the_helper_is_caught(self) -> None:
        planted = "from claude_agent_sdk import ClaudeAgentOptions\n\ndef leak():\n    return ClaudeAgentOptions()\n"

        assert _composers(planted, "planted.py") == [_Composer(location="planted.py::leak", routed=False)]

    def test_a_session_opened_without_options_is_caught(self) -> None:
        planted = "from claude_agent_sdk import ClaudeSDKClient\n\ndef leak():\n    return ClaudeSDKClient()\n"

        assert _openers_without_options(planted, "planted.py") == ["planted.py:4"]


def _assert_uncompacted(options: ClaudeAgentOptions) -> None:
    assert options.env.get("DISABLE_COMPACT") == "1"
    assert "PreCompact" in (options.hooks or {})


class TestEachHeadlessComposerRunsUncompacted(TestCase):
    def test_the_one_shot_turn(self) -> None:
        _assert_uncompacted(_clean_room_options(OneShotSpec(system_prompt="answer")))

    def test_the_dream_distiller_turn_keeps_its_credential_pin(self) -> None:
        options = _distill_options(env={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x"})

        _assert_uncompacted(options)
        assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-x"

    def test_the_dream_eval_synthesizer_turn(self) -> None:
        _assert_uncompacted(_synth_options(env=None))

    def test_the_clean_room_eval_run_keeps_its_env(self) -> None:
        workspace = Path(tempfile.mkdtemp())
        config = CleanRoomConfig(
            system_prompt="judge",
            workspace=workspace,
            cwd=str(workspace),
            env={"XDG_DATA_HOME": str(workspace)},
            allowed_tools=(),
            model="claude-opus-5",
            max_turns=3,
        )

        options = build_sdk_options(config)

        _assert_uncompacted(options)
        assert options.env["XDG_DATA_HOME"] == str(workspace)

    def test_the_ci_eval_heal_fix_turn(self) -> None:
        build = getattr(ci_eval_heal_fixer, "_fix_turn_options", None)

        assert build is not None
        _assert_uncompacted(build("fix it", cwd=Path(tempfile.mkdtemp()), env=None))
