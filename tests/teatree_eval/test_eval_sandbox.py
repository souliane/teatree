"""A declared ``fixture`` / ``cli_stubs`` sandbox reaches the agent on the non-CLI lanes.

The control every test here is written around: the SAME model issuing the SAME command
must see the stub's success line when the scenario declared it and a not-found shell
when it did not. Without that pair, a green only proves the command ran somewhere.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from teatree.eval.eval_sandbox import MAX_OUTPUT_CHARS, EvalSandbox, provision_eval_sandbox
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.pydantic_ai_runner import PydanticAiRunner, TransientThrottleTerminusError, build_eval_toolset
from teatree.eval.throttle_retry import ThrottleRetryDriver

#: The line the inert ``t3`` stub prints for the review-record verb. A model has no way
#: to produce it without the stub having genuinely run.
_STUB_LINE = "recorded verdict (bound to the reviewed head sha)"
_RECORD_COMMAND = "t3 review record MR-123 merge_safe"


def _spec(*, fixture: str = "", cli_stubs: tuple[str, ...] = ()) -> EvalSpec:
    return EvalSpec(
        name="sandbox_scenario",
        scenario="the agent acts in the world its prompt describes",
        agent_path="skills/review/SKILL.md",
        prompt="record the verdict",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        model="claude-sonnet-5",
        tools=("Bash",),
        fixture=fixture,
        cli_stubs=cli_stubs,
    )


def _last_tool_return(messages: list[ModelMessage]) -> str:
    returns = [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    return str(returns[-1]) if returns else "(no tool result)"


def _bash_then_echo(command: str) -> FunctionModel:
    """Issue one Bash call, then answer with EXACTLY what the tool returned.

    Echoing rather than narrating is what makes the assertion about the sandbox: the
    final text is the shell's own output, so it cannot be satisfied by a model that
    merely believes the command would have worked.
    """
    state = {"turn": 0}

    async def stream_fn(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        state["turn"] += 1
        if state["turn"] == 1:
            yield {0: DeltaToolCall(name="Bash", json_args=json.dumps({"command": command}))}
        else:
            yield _last_tool_return(messages)

    return FunctionModel(stream_function=stream_fn)


def _edit_then_bash() -> FunctionModel:
    state = {"turn": 0}

    async def stream_fn(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        state["turn"] += 1
        if state["turn"] == 1:
            yield {
                0: DeltaToolCall(
                    name="Edit",
                    json_args=json.dumps(
                        {"file_path": "feature_a.py", "old_string": "return 1", "new_string": "return 10"}
                    ),
                )
            }
        elif state["turn"] == 2:
            yield {0: DeltaToolCall(name="Bash", json_args=json.dumps({"command": "cat feature_a.py"}))}
        else:
            yield _last_tool_return(messages)

    return FunctionModel(stream_function=stream_fn)


def _final_text(run: EvalRun) -> str:
    return run.text_blocks[-1] if run.text_blocks else ""


class TestProvisioning:
    def test_a_scenario_declaring_nothing_gets_no_sandbox(self) -> None:
        with provision_eval_sandbox(_spec()) as sandbox:
            assert sandbox is None

    def test_a_declared_stub_resolves_on_path(self) -> None:
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            assert _STUB_LINE in sandbox.run_bash(command=_RECORD_COMMAND)

    def test_a_declared_fixture_is_the_working_directory(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            assert "feature_c.py" in sandbox.run_bash(command="git status --porcelain")

    def test_the_two_declarations_compose(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo", cli_stubs=("t3", "gh"))) as sandbox:
            assert sandbox is not None
            assert "feat: part b" in sandbox.run_bash(command="git log -1 --format=%s")
            assert _STUB_LINE in sandbox.run_bash(command=_RECORD_COMMAND)

    def test_the_shell_carries_no_model_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-shell")
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            assert "sk-should-not-reach-the-shell" not in sandbox.run_bash(command="env")


class TestCommandOutput:
    def test_a_failing_command_names_its_exit_code(self) -> None:
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            assert sandbox.run_bash(command="exit 3").startswith("exit=3\n")

    def test_a_silent_success_still_answers(self) -> None:
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            assert sandbox.run_bash(command="true") == "exit=0\n(no output)"

    def test_a_missing_command_is_empty_never_absent(self) -> None:
        assert EvalSandbox(cwd=Path(), env={}).run_bash(description="no command here") == "Bash: no command given"

    def test_a_runaway_is_truncated(self) -> None:
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            output = sandbox.run_bash(command=f"yes ok | head -c {MAX_OUTPUT_CHARS * 3}")
        assert "truncated" in output
        assert len(output) < MAX_OUTPUT_CHARS * 2


class TestFixtureEdit:
    def test_edit_changes_a_fixture_file_visible_to_bash(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            assert "Edited" in sandbox.run_edit(file_path="feature_a.py", old_string="return 1", new_string="return 10")
            assert "return 10" in sandbox.run_bash(command="cat feature_a.py")

    def test_empty_old_string_creates_only_a_new_file(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            assert "Created" in sandbox.run_edit(
                file_path="feature_d.py", old_string="", new_string="def d():\n    return 40\n"
            )
            assert "return 40" in sandbox.run_bash(command="cat feature_d.py")
            assert "already exists" in sandbox.run_edit(file_path="feature_d.py", old_string="", new_string="other")
            assert "return 40" in sandbox.run_bash(command="cat feature_d.py")

    def test_ambiguous_replacement_requires_replace_all(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            duplicate = sandbox.cwd / "duplicate.txt"
            duplicate.write_text("old\nold\n")
            assert "2 matches" in sandbox.run_edit(file_path="duplicate.txt", old_string="old", new_string="new")
            assert duplicate.read_text() == "old\nold\n"
            assert "Edited" in sandbox.run_edit(
                file_path="duplicate.txt", old_string="old", new_string="new", replace_all=True
            )
            assert duplicate.read_text() == "new\nnew\n"

    def test_edit_rejects_paths_outside_the_fixture(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("old")
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            (sandbox.cwd / "escape.txt").symlink_to(outside)
            for path in (str(outside), "../outside.txt", "escape.txt"):
                assert "outside" in sandbox.run_edit(file_path=path, old_string="old", new_string="new")
        assert outside.read_text() == "old"

    def test_edit_reports_binary_files_and_symlink_loops(self) -> None:
        with provision_eval_sandbox(_spec(fixture="git_repo")) as sandbox:
            assert sandbox is not None
            (sandbox.cwd / "binary.dat").write_bytes(b"\xff")
            (sandbox.cwd / "loop").symlink_to("loop")
            assert "could not read file" in sandbox.run_edit(file_path="binary.dat", old_string="old", new_string="new")
            assert "Edit: could not" in sandbox.run_edit(file_path="loop", old_string="old", new_string="new")


class TestThroughTheRunner:
    """The end-to-end pair: the declaration is what the agent's own probe can see."""

    def test_the_declared_stubs_output_reaches_the_agent(self) -> None:
        spec = _spec(cli_stubs=("t3",))
        run = PydanticAiRunner(model=_bash_then_echo(_RECORD_COMMAND)).run(spec)
        assert _STUB_LINE in _final_text(run)

    def test_the_same_command_is_not_found_when_nothing_was_declared(self) -> None:
        spec = _spec()
        run = PydanticAiRunner(model=_bash_then_echo(_RECORD_COMMAND)).run(spec)
        assert _STUB_LINE not in _final_text(run)

    def test_the_declared_fixture_answers_the_agents_probe(self) -> None:
        spec = _spec(fixture="git_repo")
        run = PydanticAiRunner(model=_bash_then_echo("git log --oneline -1")).run(spec)
        assert "feat: part b" in _final_text(run)

    def test_edit_then_bash_sees_the_change_in_one_fixture(self) -> None:
        spec = replace(_spec(fixture="git_repo"), tools=("Edit", "Bash"))
        run = PydanticAiRunner(model=_edit_then_bash()).run(spec)
        assert "return 10" in _final_text(run)

    def test_stub_only_sandbox_keeps_edit_inert(self) -> None:
        with provision_eval_sandbox(_spec(cli_stubs=("t3",))) as sandbox:
            assert sandbox is not None
            edit = cast("Callable[..., str]", build_eval_toolset(("Edit",), sandbox).tools["Edit"].function)
            assert edit(file_path="missing.py", old_string="", new_string="anything") == ""
            assert not (sandbox.cwd / "missing.py").exists()

    def test_throttle_retry_starts_with_a_fresh_fixture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = PydanticAiRunner(
            model=_bash_then_echo("true"),
            retry=ThrottleRetryDriver(max_attempts=1, timeout_max_attempts=0, sleep=lambda _seconds: None),
        )
        attempts = 0
        reset_observed = False

        def drive(*args: object) -> list[object]:
            nonlocal attempts, reset_observed
            sandbox = args[2]
            assert isinstance(sandbox, EvalSandbox)
            attempts += 1
            if attempts == 1:
                assert "Edited" in sandbox.run_edit(
                    file_path="feature_a.py", old_string="return 1", new_string="return 10"
                )
                throttle_message = "overloaded_error"
                raise TransientThrottleTerminusError(throttle_message)
            assert sandbox.run_bash(command="cat feature_a.py").endswith("return 1")
            reset_observed = True
            return []

        monkeypatch.setattr(runner, "_drive_once", drive)
        runner.run(_spec(fixture="git_repo"))
        assert attempts == 2
        assert reset_observed
