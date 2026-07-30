"""Headless phase-skill resolution reads the agent files interactive reads (#3667)."""

from pathlib import Path

import pytest

from teatree.agents.phase_agent_skills import (
    agent_file_name_for_phase,
    agent_names_for_phase,
    declared_skills_for_phase,
)
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.skill_support.agent_declarations import declared_skills_for_agent, default_agents_dir
from teatree.skill_support.loading import _PHASE_TO_SKILL, SkillLoadingPolicy

#: Every phase whose sub-agent is a teatree ``agents/*.md`` file. The ``codex:``
#: namespace names slash-command agents with no agent file, so it is excluded
#: exactly as the dispatch conformance guard excludes it.
_T3_PHASES = sorted({phase for (_role, phase), agent in SUBAGENT_BY_PHASE.items() if agent.startswith("t3:")})


class TestOneAgentPerPhase:
    """The pin that makes role-agnostic resolution safe."""

    @pytest.mark.parametrize("phase", _T3_PHASES)
    def test_no_phase_dispatches_two_different_agent_files(self, phase: str) -> None:
        assert len(agent_names_for_phase(phase)) == 1


class TestAgentFileNameForPhase:
    def test_resolves_the_dispatched_agent_file(self) -> None:
        assert agent_file_name_for_phase("coding") == "coder"

    def test_a_reviewer_only_phase_still_resolves(self) -> None:
        assert agent_file_name_for_phase("e2e_reviewing") == "e2e-review"

    def test_slash_command_agents_have_no_agent_file(self) -> None:
        assert agent_file_name_for_phase("codex_reviewing") == ""

    def test_phase_with_no_dispatch_row_has_no_agent_file(self) -> None:
        assert agent_file_name_for_phase("scoping") == ""


class TestDeclaredSkillsForPhase:
    def test_coding_declares_every_skill_the_coder_agent_file_lists(self) -> None:
        assert declared_skills_for_phase("coding") == declared_skills_for_agent(
            "coder", agents_dir=default_agents_dir()
        )

    def test_coding_declares_the_architecture_skill_headless_used_to_drop(self) -> None:
        assert "architecture-design" in declared_skills_for_phase("coding")

    def test_phase_with_no_agent_file_declares_nothing(self) -> None:
        assert declared_skills_for_phase("scoping") == []

    def test_the_agents_dir_is_injectable(self, tmp_path: Path) -> None:
        (tmp_path / "coder.md").write_text("---\nskills:\n  - only-this\n---\n", encoding="utf-8")
        assert declared_skills_for_phase("coding", agents_dir=tmp_path) == ["only-this"]


class TestResolutionPathEquivalence:
    """The two resolution paths must agree for every dispatched phase.

    The hard-coded map is retained only as the fallback for a phase with no
    agent file. Where an agent file DOES exist it is authoritative, so this
    turns red the moment the map and the frontmatter drift apart.
    """

    @pytest.mark.parametrize("phase", _T3_PHASES)
    def test_headless_bundle_carries_every_declared_skill(self, phase: str, tmp_path: Path) -> None:
        declared = declared_skills_for_phase(phase)
        if not declared:
            pytest.skip(f"no agent file declares skills for {phase}")
        resolved = SkillLoadingPolicy().select_for_runtime_phase(
            cwd=tmp_path,
            phase=phase,
            overlay_skill_metadata={},
            agent_declared_skills=declared,
        )
        assert set(declared) <= set(resolved.skills)

    @pytest.mark.parametrize("phase", _T3_PHASES)
    def test_fallback_map_never_names_a_skill_the_agent_file_omits(self, phase: str) -> None:
        declared = declared_skills_for_phase(phase)
        mapped = _PHASE_TO_SKILL.get(phase, "")
        if not declared or not mapped:
            pytest.skip(f"no map/agent-file pair to compare for {phase}")
        assert mapped in declared, (
            f"phase {phase!r} maps to {mapped!r} but agents/{agent_file_name_for_phase(phase)}.md declares {declared}"
        )
