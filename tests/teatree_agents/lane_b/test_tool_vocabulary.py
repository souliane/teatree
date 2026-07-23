"""Lane B exposes the SKILL/SDK tool vocabulary (``Bash``/``Read``/…), not ``shell``.

A loaded skill says ``Bash`` / ``Read`` / ``Write`` / ``Edit`` / ``Grep``; Lane B used
to expose ``shell`` / ``read_file`` / … so a skill instruction did not name a real
tool. The capability names stay the neutral SSOT (``phase_tools``); Lane B maps each
to the model-visible skill name at its own boundary, mirroring Lane A's
``sdk_tool_map`` — and every Lane B display name is the PRIMARY SDK name for the same
capability, so Lane A is provably unbroken.
"""

import asyncio
from pathlib import Path

import pydantic_ai.models
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.filesystem import build_filesystem_toolset
from teatree.agents.lane_b.shell import build_shell_toolset
from teatree.agents.lane_b.tool_names import CAPABILITY_TO_LANE_B_TOOL, lane_b_tool_name
from teatree.agents.lane_b.toolsets import build_lane_b_toolsets
from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS, sdk_disallowed_tools_for_phase

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # ty: ignore[invalid-assignment] — zero-token guard.

_SKILL_VOCABULARY = {"Bash", "Read", "Write", "Edit", "Grep"}


class TestCapabilityToolsUseSkillNames:
    def test_shell_tool_is_named_bash(self, tmp_path: Path) -> None:
        assert "Bash" in build_shell_toolset(LaneBToolConfig(fs_root=tmp_path)).tools
        assert "shell" not in build_shell_toolset(LaneBToolConfig(fs_root=tmp_path)).tools

    def test_filesystem_tools_use_skill_names(self, tmp_path: Path) -> None:
        names = set(build_filesystem_toolset(tmp_path).tools)
        assert {"Read", "Write", "Edit", "Grep"} <= names
        assert not ({"read_file", "write_file", "edit_file", "search_files"} & names)


class TestAssembledCodingPhaseExposesSkillVocabulary:
    def test_coding_phase_tool_names_are_the_skill_vocabulary(self, tmp_path: Path) -> None:
        exposed: set[str] = set()

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            exposed.update(t.name for t in info.function_tools)
            return ModelResponse(parts=[TextPart(content="done")])

        config = LaneBToolConfig(fs_root=tmp_path, phase="coding")
        agent = Agent[None, str](FunctionModel(model_fn), toolsets=build_lane_b_toolsets(config).toolsets)
        asyncio.run(agent.run("go"))
        assert exposed >= _SKILL_VOCABULARY, _SKILL_VOCABULARY - exposed


class TestReviewPhaseStillHidesWriteUnderNewNames:
    def test_review_phase_exposes_read_and_bash_but_not_write(self, tmp_path: Path) -> None:
        exposed: set[str] = set()

        def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            exposed.update(t.name for t in info.function_tools)
            return ModelResponse(parts=[TextPart(content="done")])

        config = LaneBToolConfig(fs_root=tmp_path, phase="reviewing")
        agent = Agent[None, str](FunctionModel(model_fn), toolsets=build_lane_b_toolsets(config).toolsets)
        asyncio.run(agent.run("go"))
        assert {"Read", "Grep", "Bash"} <= exposed
        assert "Write" not in exposed
        assert "Edit" not in exposed


class TestLaneAParity:
    def test_every_lane_b_display_name_is_the_primary_sdk_name(self) -> None:
        # Lane B's single display name for a capability must be one of Lane A's SDK
        # names for the same capability — the two boundaries share one vocabulary.
        for capability, display in CAPABILITY_TO_LANE_B_TOOL.items():
            assert display in CAPABILITY_TO_SDK_TOOLS[capability], capability

    def test_lane_a_review_disallow_is_unbroken(self) -> None:
        # The rename touches no Lane-A boundary: a review phase still denies the SDK
        # write/edit/bash names exactly as before.
        denied = set(sdk_disallowed_tools_for_phase("reviewing"))
        assert {"Write", "Edit"} <= denied
        assert "Bash" not in denied  # a reviewer keeps the shell

    def test_lane_b_name_helper_is_identity_for_unmapped(self) -> None:
        assert lane_b_tool_name("web_fetch") == "web_fetch"
        assert lane_b_tool_name("shell") == "Bash"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
