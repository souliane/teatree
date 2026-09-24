"""Toolset-assembly integration — a scripted FunctionModel drives the real tools.

Zero-token: ``ALLOW_MODEL_REQUESTS = False`` proves no network/model call escapes;
the scripted :class:`FunctionModel` supplies every model turn.
"""

import asyncio
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pydantic_ai.models
import pytest
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.combined import CombinedToolset

from teatree.agents import skill_injection
from teatree.agents._runner_options import _disallowed_tools_for_phase
from teatree.agents.lane_b import toolsets as toolsets_module
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.gating import HardDenyToolset
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.prompt import build_system_context
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.skill_files import SkillFileIndex
from teatree.agents.skill_injection import harness_skills_dirs
from teatree.core.modelkit.phase_tools import tools_for_phase
from teatree.core.modelkit.phases import KNOWN_PHASES
from teatree.core.models import Session, Task, Ticket
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.types import SkillMetadata
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # the zero-token test guard.

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_SKILLS = _REPO_ROOT / "skills"


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
            _call("Write", {"path": "out.txt", "content": "hi"}),
            _text("wrote it"),
        )
        result = _run(_agent(config, model), "go")
        assert result.output == "wrote it"
        assert (tmp_path / "out.txt").read_text() == "hi"


class TestPhaseScopedToolsHidden:
    def test_review_phase_does_not_expose_write_file(self, tmp_path: Path) -> None:
        config = LaneBToolConfig(fs_root=tmp_path, phase="reviewing")
        model = _scripted(_call("Write", {"path": "x", "content": "y"}), _text("ok"))
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
            _call("Bash", {"command": "git reset --hard HEAD~1"}),
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
        model = _scripted(_call("Bash", {"command": "git reset --hard HEAD"}), _text("done"))
        result = _run(_agent(config, model), "go")
        assert result.output == "done"
        assert not [
            p for m in result.all_messages() for p in getattr(m, "parts", []) if type(p).__name__ == "RetryPromptPart"
        ]

    def test_safe_shell_command_runs(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("")
        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        model = _scripted(_call("Bash", {"command": "ls"}), _text("listed"))
        result = _run(_agent(config, model), "go")
        returns = [
            p for m in result.all_messages() for p in getattr(m, "parts", []) if type(p).__name__ == "ToolReturnPart"
        ]
        assert any("marker" in str(p.content) for p in returns)


class TestMcpHonoursEmptyPhaseAllowance:
    def _pin_sentinel_mcp(self, monkeypatch: pytest.MonkeyPatch) -> AbstractToolset[None]:
        sentinel: AbstractToolset[None] = CombinedToolset([])
        monkeypatch.setattr(toolsets_module, "build_mcp_toolsets", lambda: [sentinel])
        return sentinel

    def test_none_phase_gets_no_mcp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # short_describe maps to the empty (NON-None) allowance — it may call NOTHING,
        # so no MCP toolset may attach past the phase filter.
        sentinel = self._pin_sentinel_mcp(monkeypatch)
        config = LaneBToolConfig(fs_root=tmp_path, phase="short_describe")
        assert sentinel not in build_lane_b_toolsets(config).toolsets

    def test_work_phase_still_gets_mcp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = self._pin_sentinel_mcp(monkeypatch)
        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        assert sentinel in build_lane_b_toolsets(config).toolsets


class TestMaxDenialsThreadedFromConfig:
    """``build_lane_b_toolsets`` wires ``config.max_denials`` into the assembled toolset."""

    def test_exploration_phase_widens_the_assembled_cap(self, tmp_path: Path) -> None:
        config = LaneBToolConfig(fs_root=tmp_path, phase="architectural_review")
        gated = build_lane_b_toolsets(config).toolsets[0]
        assert isinstance(gated, HardDenyToolset)
        assert gated.max_denials == config.max_denials
        assert gated.max_denials > 3

    def test_other_phase_keeps_the_tight_assembled_cap(self, tmp_path: Path) -> None:
        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        gated = build_lane_b_toolsets(config).toolsets[0]
        assert isinstance(gated, HardDenyToolset)
        assert gated.max_denials == 3


_SKILL_PATH_RE = re.compile(r"(?<![\w.-])skills/[a-z0-9_-]+/(?:SKILL\.md|references/[A-Za-z0-9_.-]+\.md)")
_QUARANTINED_PHASES = ("directive_reading", "short_describe")


def _rendered_context(phase: str) -> str:
    ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{abs(hash(phase)) % 100_000}")
    task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
    skills = resolve_skill_bundle(phase=phase, overlay_skill_metadata=SkillMetadata(), worktree_path=_REPO_ROOT)
    return build_system_context(task, skills=skills, lifecycle_skill=SkillLoadingPolicy.lifecycle_for_phase(phase))


def _named_skill_files(context: str) -> set[str]:
    """Each existing skill file the context names, as its repo-relative ``skills/<skill>/...`` key."""
    return {key for key in _SKILL_PATH_RE.findall(context) if (_REPO_SKILLS / key.removeprefix("skills/")).is_file()}


def _both_spellings(key: str) -> tuple[str, str]:
    return key, str(_REPO_SKILLS / key.removeprefix("skills/"))


def _read_all(config: LaneBToolConfig, paths: list[str]) -> dict[str, str]:
    """Issue one ``Read`` per path in a single model turn; map each path to its tool reply."""
    calls = [ToolCallPart(tool_name="Read", args={"path": path}, tool_call_id=f"c{i}") for i, path in enumerate(paths)]
    model = _scripted(lambda: ModelResponse(parts=calls), _text("read"))
    result = _run(_agent(config, model), "go")
    by_id = {
        p.tool_call_id: str(p.content)
        for m in result.all_messages()
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "ToolReturnPart"
    }
    return {path: by_id.get(f"c{i}", "<no tool return>") for i, path in enumerate(paths)}


def _read_one(config: LaneBToolConfig, path: str) -> tuple[list[str], list[str]]:
    """``(tool returns, retry prompts)`` for a single scripted ``Read`` of *path*."""
    model = _scripted(_call("Read", {"path": path}), _text("done"))
    result = _run(_agent(config, model), "go")
    parts = [p for m in result.all_messages() for p in getattr(m, "parts", [])]
    returns = [str(p.content) for p in parts if type(p).__name__ == "ToolReturnPart"]
    retries = [str(p.content) for p in parts if type(p).__name__ == "RetryPromptPart"]
    return returns, retries


def _tool_names(config: LaneBToolConfig) -> set[str]:
    seen: set[str] = set()

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    _run(_agent(config, FunctionModel(model_fn)), "go")
    return seen


_REFUSED_WITHOUT_A_WORKTREE = (
    "/etc/hostname",
    "skills/rules/references/../SKILL.md",
    "skills/../pyproject.toml",
    "skills/rules",
    "skills",
    "skills/rulesX/SKILL.md",
    "skills/rules/SKILL.md.bak",
    "skills/rules/references/notes.txt",
    str(_REPO_SKILLS),
    str(_REPO_SKILLS / "rules" / "references"),
    f"{_REPO_SKILLS}/rules/references/../SKILL.md",
)


class TestSkillFilesTheContextNamesAreReadable(TestCase):
    """Every skill file a phase's rendered context names is readable by that phase on Lane B."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        (home := self.scratch / "empty-home").mkdir()
        for patcher in (
            patch.dict(os.environ, {"HOME": str(home)}),
            patch.object(toolsets_module, "build_mcp_toolsets", list),
            patch.object(skill_injection, "DEFAULT_SKILLS_DIR", _REPO_SKILLS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _reading_phases(self) -> dict[str, set[str]]:
        named = {phase: _named_skill_files(_rendered_context(phase)) for phase in sorted(KNOWN_PHASES)}
        return {phase: keys for phase, keys in named.items() if keys and "read_file" in tools_for_phase(phase)}

    def test_every_named_skill_file_reads_back_with_and_without_a_worktree(self) -> None:
        phases = self._reading_phases()
        assert phases, "no phase names a skill file — the path extraction went blind"
        worktree = self.scratch / "not-a-teatree-checkout"
        worktree.mkdir()
        unreadable: list[str] = []
        for phase, keys in phases.items():
            expected = {
                spelling: (_REPO_SKILLS / key.removeprefix("skills/")).read_text(encoding="utf-8")
                for key in keys
                for spelling in _both_spellings(key)
            }
            for fs_root in (worktree, None):
                replies = _read_all(LaneBToolConfig(fs_root=fs_root, phase=phase), sorted(expected))
                unreadable.extend(
                    f"{phase} fs_root={fs_root}: {path}" for path, reply in replies.items() if reply != expected[path]
                )
        assert not unreadable, "named skill files a phase cannot Read:\n" + "\n".join(unreadable[:40])

    def test_quarantined_phases_get_no_read(self) -> None:
        for phase in _QUARANTINED_PHASES:
            for fs_root in (self.scratch, None):
                with self.subTest(phase=phase, fs_root=fs_root):
                    assert "Read" not in _tool_names(LaneBToolConfig(fs_root=fs_root, phase=phase))

    def test_a_worktree_copy_shadows_the_registered_skill_file(self) -> None:
        key = "skills/rules/SKILL.md"
        (self.scratch / key).parent.mkdir(parents=True)
        (self.scratch / key).write_text("worktree copy", encoding="utf-8")
        returns, _ = _read_one(LaneBToolConfig(fs_root=self.scratch, phase="coding"), key)
        assert returns == ["worktree copy"]

    def test_anything_but_a_registered_skill_file_is_refused_without_a_worktree(self) -> None:
        for path in _REFUSED_WITHOUT_A_WORKTREE:
            with self.subTest(path=path):
                returns, retries = _read_one(LaneBToolConfig(fs_root=None, phase="retro"), path)
                assert not returns
                assert retries

    def test_a_worktree_dispatch_cannot_escape_through_the_fallback(self) -> None:
        for path in ("/etc/hostname", "../../../etc/hostname", str(_REPO_SKILLS.parent / "pyproject.toml")):
            with self.subTest(path=path):
                returns, retries = _read_one(LaneBToolConfig(fs_root=self.scratch, phase="coding"), path)
                assert not returns
                assert retries

    def test_lane_a_keeps_read_for_every_reading_phase_and_every_named_path_is_registered(self) -> None:
        index = SkillFileIndex.build(harness_skills_dirs())
        for phase, keys in self._reading_phases().items():
            assert "Read" not in _disallowed_tools_for_phase(phase), phase
            for key in keys:
                absolute = _both_spellings(key)[1]
                assert index.lookup(absolute) == Path(absolute), f"{phase}: {absolute} is not a registered skill file"


def test_no_model_requests_are_allowed() -> None:
    # Anti-vacuity guard: the scripted lane must never fall through to a real call.
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
