from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents import skill_assurance, skill_injection


@pytest.fixture(autouse=True)
def installed_agent_test_skills() -> Iterator[None]:
    # Real fixture bodies satisfy strict preflight without depending on the developer's installed stack skills.
    fixture_skills = Path(__file__).parents[1] / "fixtures" / "agent_skills"
    original_roots = skill_injection.harness_skills_dirs

    def skill_dirs() -> list[Path]:
        return [fixture_skills, *original_roots()]

    with (
        patch.object(skill_injection, "harness_skills_dirs", side_effect=skill_dirs),
        patch.object(skill_assurance, "harness_skills_dirs", side_effect=skill_dirs),
    ):
        yield
