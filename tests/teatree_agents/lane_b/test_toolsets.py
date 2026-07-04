"""Toolset-assembly integration — a scripted FunctionModel drives the real tools.

Zero-token: ``ALLOW_MODEL_REQUESTS = False`` proves no network/model call escapes;
the scripted :class:`FunctionModel` supplies every model turn.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pydantic_ai.models
import pytest
from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # ty: ignore[invalid-assignment] — the zero-token test guard.


def _run(agent: Agent[None, str], prompt: str) -> AgentRunResult[Any]:
    return asyncio.run(agent.run(prompt))


def _scripted(*turns: Callable[[], ModelResponse]) -> FunctionModel:
    """A FunctionModel that plays *turns* in order, one per model request."""
    state = {"i": 0}

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = turns[min(state["i"], len(turns) - 1)]
        state["i"] += 1
        return turn()

    return FunctionModel(model_fn)


def _call(name: str, args: dict, cid: str = "c1") -> Callable[[], ModelResponse]:
    return lambda: ModelResponse(parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=cid)])


def _text(text: str) -> Callable[[], ModelResponse]:
    return lambda: ModelResponse(parts=[TextPart(content=text)])


def _agent(config: LaneBToolConfig, model: FunctionModel) -> Agent[None, str]:
    return Agent[None, str](model, toolsets=build_lane_b_toolsets(config).toolsets)


class TestPhaseScopedToolsExecute:
    def test_coding_phase_can_write_and_read(self, tmp_path: Path) -> None:
        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        model = _scripted(
            _call("write_file", {"path": "out.txt", "content": "hi"}),
            _text("wrote it"),
        )
        result = _run(_agent(config, model), "go")
        assert result.output == "wrote it"
        assert (tmp_path / "out.txt").read_text() == "hi"


class TestPhaseScopedToolsHidden:
    def test_review_phase_does_not_expose_write_file(self, tmp_path: Path) -> None:
        config = LaneBToolConfig(fs_root=tmp_path, phase="reviewing")
        model = _scripted(_call("write_file", {"path": "x", "content": "y"}), _text("ok"))
        # write_file is not in the review-phase allowance → the model's call finds
        # no such tool and is retried; it must NOT have written the file.
        _run(_agent(config, model), "go")
        assert not (tmp_path / "x").exists()


class TestHardDenyWithinAssembledToolsets:
    def test_main_clone_mutation_is_refused_and_never_runs(self, tmp_path: Path) -> None:
        # The jail root (fs_root) is a managed main clone → the mutation is denied.
        clone = managed_main_clone(tmp_path / "teatree")
        config = LaneBToolConfig(fs_root=clone, phase="coding")
        model = _scripted(
            _call("shell", {"command": "git reset --hard HEAD~1"}),
            _text("understood, i will not"),
        )
        result = _run(_agent(config, model), "go")
        assert result.output == "understood, i will not"
        # The refusal surfaced as a RetryPromptPart carrying the deny reason.
        retries = [
            p for m in result.all_messages() for p in getattr(m, "parts", []) if type(p).__name__ == "RetryPromptPart"
        ]
        assert any("BLOCKED" in str(p.content) for p in retries)

    def test_same_mutation_runs_when_jailed_to_a_linked_worktree(self, tmp_path: Path) -> None:
        # The jail root is a WORKTREE → the same op is allowed and executes.
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        config = LaneBToolConfig(fs_root=wt, phase="coding")
        model = _scripted(_call("shell", {"command": "git reset --hard HEAD"}), _text("done"))
        result = _run(_agent(config, model), "go")
        assert result.output == "done"
        assert not [
            p for m in result.all_messages() for p in getattr(m, "parts", []) if type(p).__name__ == "RetryPromptPart"
        ]

    def test_safe_shell_command_runs(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("")
        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        model = _scripted(_call("shell", {"command": "ls"}), _text("listed"))
        result = _run(_agent(config, model), "go")
        returns = [
            p for m in result.all_messages() for p in getattr(m, "parts", []) if type(p).__name__ == "ToolReturnPart"
        ]
        assert any("marker" in str(p.content) for p in returns)


def test_no_model_requests_are_allowed() -> None:
    # Anti-vacuity guard: the scripted lane must never fall through to a real call.
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
