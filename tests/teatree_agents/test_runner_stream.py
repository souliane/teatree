"""A run's stream capture, and which endings count as a run cut short."""

from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock

from teatree.agents.codex_app_server_messages import tool_blocks
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture
from tests.teatree_agents._sdk_fake import assistant_text, assistant_tool_use, rate_limit_event, result_message


class TestStreamCapture:
    def test_it_hands_over_everything_observed_before_the_run_was_cut_off(self) -> None:
        capture = StreamCapture()
        for message in (
            assistant_tool_use(),
            assistant_text("first"),
            rate_limit_event("five_hour"),
            assistant_text("second"),
        ):
            capture.observe(message)

        outcome = capture.outcome(stuck_reason="runtime ceiling exceeded")

        assert outcome.agent_text == "first\nsecond"
        assert outcome.tool_calls == 1
        assert outcome.rate_limit_info is not None
        assert outcome.result_message is None
        assert outcome.stuck_reason == "runtime ceiling exceeded"

    def test_a_window_that_was_not_rejected_is_not_kept(self) -> None:
        capture = StreamCapture()

        capture.observe(rate_limit_event("five_hour", status="allowed"))

        assert capture.outcome().rate_limit_info is None

    def test_the_terminal_result_is_kept(self) -> None:
        capture = StreamCapture()

        capture.observe(result_message(session_id="s1"))

        result = capture.outcome().result_message
        assert result is not None
        assert result.session_id == "s1"

    def test_skill_tool_receipt_captures_only_the_requested_name(self) -> None:
        capture = StreamCapture()
        capture.observe(
            AssistantMessage(
                content=[ToolUseBlock(id="s1", name="Skill", input={"skill": "t3:code", "secret": "omit"})],
                model="claude-opus-4-8[1m]",
            )
        )
        assert capture.outcome().observed_skill_loads == ()
        capture.observe(AssistantMessage(content=[ToolResultBlock(tool_use_id="s1", content="loaded")], model=""))

        assert capture.outcome().observed_skill_loads == ("code",)

    def test_full_skill_file_read_is_an_observed_load_without_retaining_its_path(self, tmp_path: Path) -> None:
        skill_path = tmp_path / "code" / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text("Complete skill instructions\nsecond line\n", encoding="utf-8")
        capture = StreamCapture()
        capture.observe(
            AssistantMessage(
                content=[ToolUseBlock(id="r1", name="Read", input={"file_path": str(skill_path)})],
                model="claude-opus-4-8[1m]",
            )
        )
        with patch("teatree.agents.runner_stream.harness_skills_dirs", return_value=[tmp_path]):
            capture.observe(
                AssistantMessage(
                    content=[ToolResultBlock(tool_use_id="r1", content=skill_path.read_text(encoding="utf-8"))],
                    model="",
                )
            )

        assert capture.outcome().observed_skill_loads == ("code",)

    def test_codex_cat_of_full_skill_body_is_an_observed_load(self, tmp_path: Path) -> None:
        skill_path = tmp_path / "code" / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text("Complete skill instructions\nsecond line\n", encoding="utf-8")
        capture = StreamCapture()
        with patch("teatree.agents.runner_stream.harness_skills_dirs", return_value=[tmp_path]):
            capture.observe(
                AssistantMessage(
                    content=tool_blocks(
                        {
                            "id": "c1",
                            "type": "commandExecution",
                            "command": f"cat {skill_path}",
                            "aggregatedOutput": skill_path.read_text(encoding="utf-8"),
                            "exitCode": 0,
                            "status": "completed",
                        }
                    ),
                    model="",
                )
            )

        assert capture.outcome().observed_skill_loads == ("code",)

    def test_codex_shell_output_without_a_direct_full_skill_read_is_not_evidence(self, tmp_path: Path) -> None:
        skill_path = tmp_path / "code" / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text("Complete skill instructions\nsecond line\n", encoding="utf-8")
        capture = StreamCapture()
        for tool_id, command in (("partial", f"cat {skill_path}"), ("other", f"echo {skill_path}")):
            capture.observe(
                AssistantMessage(content=[ToolUseBlock(id=tool_id, name="Bash", input={"command": command})], model="")
            )
        with patch("teatree.agents.runner_stream.harness_skills_dirs", return_value=[tmp_path]):
            capture.observe(
                AssistantMessage(
                    content=[ToolResultBlock(tool_use_id="partial", content="Complete skill instructions\n")], model=""
                )
            )
            capture.observe(
                AssistantMessage(
                    content=[ToolResultBlock(tool_use_id="other", content=skill_path.read_text(encoding="utf-8"))],
                    model="",
                )
            )

        assert capture.outcome().observed_skill_loads == ()

    def test_partial_or_wrong_skill_read_does_not_claim_a_load(self, tmp_path: Path) -> None:
        expected = tmp_path / "code" / "SKILL.md"
        expected.parent.mkdir()
        expected.write_text("First line\nSecond line\n", encoding="utf-8")
        wrong = tmp_path / "other" / "code" / "SKILL.md"
        wrong.parent.mkdir(parents=True)
        wrong.write_text(expected.read_text(encoding="utf-8"), encoding="utf-8")
        capture = StreamCapture()
        for tool_id, path in (("partial", expected), ("wrong", wrong)):
            capture.observe(
                AssistantMessage(
                    content=[ToolUseBlock(id=tool_id, name="Read", input={"file_path": str(path)})], model=""
                )
            )
        with patch("teatree.agents.runner_stream.harness_skills_dirs", return_value=[tmp_path]):
            capture.observe(
                AssistantMessage(content=[ToolResultBlock(tool_use_id="partial", content="First line\n")], model="")
            )
            capture.observe(
                AssistantMessage(
                    content=[ToolResultBlock(tool_use_id="wrong", content="First line\nSecond line\n")], model=""
                )
            )

        assert capture.outcome().observed_skill_loads == ()

    def test_failed_or_unmatched_tool_result_does_not_claim_a_skill_load(self) -> None:
        capture = StreamCapture()
        capture.observe(
            AssistantMessage(content=[ToolUseBlock(id="s1", name="Skill", input={"skill": "code"})], model="")
        )
        capture.observe(AssistantMessage(content=[ToolResultBlock(tool_use_id="other", content="loaded")], model=""))
        capture.observe(
            AssistantMessage(content=[ToolResultBlock(tool_use_id="s1", content="not found", is_error=True)], model="")
        )

        assert capture.outcome().observed_skill_loads == ()


def _outcome(*, result: ResultMessage | None = None, stuck_reason: str | None = None, **flags: bool) -> HarnessOutcome:
    return HarnessOutcome(agent_text="", result_message=result, stuck_reason=stuck_reason, **flags)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_outcome(stuck_reason="runtime ceiling exceeded"), id="runtime-ceiling"),
        pytest.param(_outcome(result=result_message(subtype="error_max_turns", is_error=True)), id="turn-ceiling"),
        pytest.param(_outcome(result=result_message(is_error=True, result="Prompt is too long")), id="context-full"),
        pytest.param(_outcome(compaction_stopped=True), id="compaction-blocked"),
    ],
)
def test_a_ceiling_a_full_window_or_a_blocked_compaction_cuts_a_run_short(outcome: HarnessOutcome) -> None:
    assert outcome.cut_short


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_outcome(result=result_message()), id="clean-result"),
        pytest.param(_outcome(result=result_message(is_error=True, result="boom")), id="ordinary-error"),
        pytest.param(_outcome(stuck_reason="lease lost for task 1", lease_lost=True), id="lease-lost"),
    ],
)
def test_a_clean_run_an_ordinary_error_or_a_lost_lease_is_not_cut_short(outcome: HarnessOutcome) -> None:
    assert not outcome.cut_short
