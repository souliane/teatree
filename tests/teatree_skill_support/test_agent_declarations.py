"""Agent-file skill declarations are the ONE source both lanes resolve from (#3667)."""

from pathlib import Path

import pytest

from teatree.skill_support.agent_declarations import (
    agent_declared_skills,
    declared_skills_for_agent,
    default_agents_dir,
)


class TestAgentDeclaredSkills:
    def test_reads_the_frontmatter_skills_list(self, tmp_path: Path) -> None:
        agent = tmp_path / "coder.md"
        agent.write_text(
            "---\nname: coder\ntools:\n  - Read\nskills:\n  - rules\n  - architecture-design\n---\n\n# Coder\n",
            encoding="utf-8",
        )
        assert agent_declared_skills(agent) == ["rules", "architecture-design"]

    def test_stops_at_the_closing_frontmatter_fence(self, tmp_path: Path) -> None:
        agent = tmp_path / "a.md"
        agent.write_text(
            "---\nskills:\n  - rules\n---\n\nskills:\n  - not-a-declaration\n",
            encoding="utf-8",
        )
        assert agent_declared_skills(agent) == ["rules"]

    def test_absent_file_declares_nothing(self, tmp_path: Path) -> None:
        assert agent_declared_skills(tmp_path / "missing.md") == []

    def test_file_without_frontmatter_declares_nothing(self, tmp_path: Path) -> None:
        agent = tmp_path / "a.md"
        agent.write_text("# No frontmatter\nskills:\n  - rules\n", encoding="utf-8")
        assert agent_declared_skills(agent) == []

    def test_resolves_an_agent_by_name(self, tmp_path: Path) -> None:
        (tmp_path / "tester.md").write_text("---\nskills:\n  - test\n---\n", encoding="utf-8")
        assert declared_skills_for_agent("tester", agents_dir=tmp_path) == ["test"]

    def test_unknown_agent_name_declares_nothing(self, tmp_path: Path) -> None:
        assert declared_skills_for_agent("nope", agents_dir=tmp_path) == []


class TestShippedAgentFiles:
    def test_coder_declares_the_architecture_skill(self) -> None:
        assert "architecture-design" in declared_skills_for_agent("coder", agents_dir=default_agents_dir())

    def test_orchestrator_declares_the_architecture_skill(self) -> None:
        assert "architecture-design" in declared_skills_for_agent("orchestrator", agents_dir=default_agents_dir())

    @pytest.mark.parametrize("agent", ["coder", "reviewer", "tester", "planner", "shipper"])
    def test_every_dispatched_agent_declares_the_cross_cutting_rules(self, agent: str) -> None:
        assert "rules" in declared_skills_for_agent(agent, agents_dir=default_agents_dir())
