from pathlib import Path

import pytest

from teatree.eval.agent_prompt import load_agent_definition, scenario_system_prompt
from teatree.eval.models import EvalSpec
from teatree.eval.prompt_framing import LIVE_ENV_FRAMING


def test_prompt_contains_only_the_graded_section_and_live_framing(tmp_path: Path) -> None:
    agent = tmp_path / "SKILL.md"
    agent.write_text("## Target\nrequired behavior\n\n## Unrelated\nother behavior\n")
    spec = EvalSpec(
        name="case",
        scenario="scenario",
        agent_path="SKILL.md",
        agent_sections=("Target",),
        prompt="do",
        matchers=(),
        source_path=tmp_path / "case.yaml",
    )
    prompt = scenario_system_prompt(spec)
    assert "required behavior" in prompt
    assert "other behavior" not in prompt
    assert LIVE_ENV_FRAMING in prompt


def test_missing_agent_definition_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Agent definition not found"):
        load_agent_definition("missing.md", spec_dir=tmp_path)
