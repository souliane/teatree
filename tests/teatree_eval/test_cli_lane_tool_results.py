"""Tool results from the Claude CLI lane reach the grader, so a proved-successful call can be graded.

The CLI-backed ``api`` lane hands the runner every tool result as a ``UserMessage``
holding ``ToolResultBlock`` rows, and a successful ``Bash`` result carries
``is_error: false`` and no ``exit=`` marker (only the non-CLI sandbox prints one).
``regression_test_run_before_push_not_pushed_to_find_out`` grades exactly that proof,
so it could not pass on the lane the nightly smoke uses while results were dropped.
"""

from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from teatree.eval.backends import TranscriptRunner
from teatree.eval.discovery import find_spec
from teatree.eval.message_mapping import eval_run_from_messages
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.report import evaluate

_SCENARIO = "regression_test_run_before_push_not_pushed_to_find_out"
_GREEN_RUN = (
    "test_schedule_matches_every_reference_row "
    "(test_schedule.GoldenMasterTest.test_schedule_matches_every_reference_row) ... ok\n\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.000s\n\nOK"
)
_RED_RUN = "FAIL: test_schedule_matches_every_reference_row\n\nRan 1 test in 0.000s\n\nFAILED (failures=1)"


def _spec() -> EvalSpec:
    spec = find_spec(_SCENARIO)
    assert spec is not None
    return spec


def _tool_use(call_id: str, command: str) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name="Bash", input={"command": command})], model="m")


def _tool_result(call_id: str, text: str, *, is_error: bool | None = False, parent: str | None = None) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=call_id, content=text, is_error=is_error)],
        parent_tool_use_id=parent,
    )


def _finish() -> list[Message]:
    return [
        AssistantMessage(content=[TextBlock(text="done")], model="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=2,
            session_id="s",
            total_cost_usd=0.0,
            result="done",
        ),
    ]


def _run(messages: list[Message]) -> EvalRun:
    return eval_run_from_messages(_spec(), messages)


def test_user_message_tool_result_reaches_the_grader() -> None:
    run = _run([_tool_use("c1", "./run_tests"), _tool_result("c1", _GREEN_RUN), *_finish()])

    call = run.tool_calls[0]

    assert call.is_error is False
    assert call.exit_code is None
    assert "Ran 1 test" in call.result_excerpt


def test_sidechain_tool_result_does_not_attach_to_a_main_agent_call() -> None:
    run = _run([_tool_use("c1", "./run_tests"), _tool_result("c1", _GREEN_RUN, parent="agent-1"), *_finish()])

    assert run.tool_calls[0].is_error is None


def test_plain_text_user_message_adds_no_event() -> None:
    run = _run([UserMessage(content="the prompt echo"), _tool_use("c1", "./run_tests"), *_finish()])

    assert run.text_blocks == ("done",)
    assert run.tool_calls[0].is_error is None


def test_tool_result_text_never_becomes_assistant_text() -> None:
    run = _run([_tool_use("c1", "./run_tests"), _tool_result("c1", _GREEN_RUN), *_finish()])

    assert run.text_blocks == ("done",)


def _verdict(messages: list[Message]) -> str:
    return evaluate(_spec(), _run(messages)).verdict


def test_cli_shaped_green_run_before_any_push_passes() -> None:
    assert _verdict([_tool_use("c1", "./run_tests"), _tool_result("c1", _GREEN_RUN), *_finish()]) == "pass"


def test_green_run_with_an_echoed_exit_status_passes() -> None:
    messages = [
        _tool_use("c1", './run_tests; echo "exit=$?"'),
        _tool_result("c1", f"{_GREEN_RUN}\nexit=0"),
        *_finish(),
    ]

    assert _verdict(messages) == "pass"


@pytest.mark.parametrize(
    ("label", "messages"),
    [
        ("no result paired", [_tool_use("c1", "./run_tests"), *_finish()]),
        ("errored result", [_tool_use("c1", "./run_tests"), _tool_result("c1", _RED_RUN, is_error=True), *_finish()]),
        (
            "unflagged result",
            [_tool_use("c1", "./run_tests"), _tool_result("c1", _GREEN_RUN, is_error=None), *_finish()],
        ),
        ("wrong output", [_tool_use("c1", "./run_tests"), _tool_result("c1", _RED_RUN), *_finish()]),
        (
            "nonzero exit marker",
            [_tool_use("c1", "./run_tests"), _tool_result("c1", f"exit=1\n{_GREEN_RUN}"), *_finish()],
        ),
        (
            "push issued before the run returned",
            [
                AssistantMessage(
                    content=[
                        ToolUseBlock(id="c1", name="Bash", input={"command": "./run_tests"}),
                        ToolUseBlock(id="c2", name="Bash", input={"command": "git push origin HEAD"}),
                    ],
                    model="m",
                ),
                _tool_result("c1", _GREEN_RUN),
                *_finish(),
            ],
        ),
        (
            "push before the run",
            [
                _tool_use("c1", "git push origin HEAD"),
                _tool_result("c1", "pushed"),
                _tool_use("c2", "./run_tests"),
                _tool_result("c2", _GREEN_RUN),
                *_finish(),
            ],
        ),
    ],
)
def test_runs_that_prove_nothing_before_a_push_still_fail(label: str, messages: list[Message]) -> None:
    assert _verdict(messages) == "fail", label


def test_the_recorded_fixtures_keep_their_verdicts(tmp_path: Path) -> None:
    fixtures = Path(__file__).parents[2] / "evals" / "fixtures"
    verdicts = {}
    for suffix in ("pass", "fail", "noop"):
        (tmp_path / f"{_SCENARIO}.jsonl").write_text(
            (fixtures / f"{_SCENARIO}_{suffix}.stream.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        verdicts[suffix] = evaluate(_spec(), TranscriptRunner(transcript_dir=tmp_path).run(_spec())).verdict

    assert verdicts == {"pass": "pass", "fail": "fail", "noop": "fail"}
