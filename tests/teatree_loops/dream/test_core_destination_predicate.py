"""The shared core-fix destination classifier (#61 nit).

``points_at_core_fix`` is the ONE home for "does this destination name a
teatree-core fix path?" — Pass-2 triage and the compliance recurrence redirect
(``_is_memory_only``) both delegate to it instead of each re-implementing the
``strip().lower().startswith(prefixes)`` check.
"""

import pytest

from teatree.loops.dream.compliance import _is_memory_only
from teatree.loops.dream.promote_memory import points_at_core_fix


@pytest.mark.parametrize(
    "destination",
    ["src/teatree/loop/tick.py", "skills/ship/SKILL.md", "  SCRIPTS/hooks/x.py  ", "blueprint.md", "agents/harness.py"],
)
def test_core_fix_paths_are_recognised_case_and_whitespace_insensitively(destination: str) -> None:
    assert points_at_core_fix(destination) is True


@pytest.mark.parametrize("destination", ["feedback_tone.md", "memory/topic.md", "", "   "])
def test_non_core_paths_are_not_core_fixes(destination: str) -> None:
    assert points_at_core_fix(destination) is False


@pytest.mark.parametrize(
    "destination",
    ["src/teatree/loop/tick.py", "skills/ship/SKILL.md", "feedback_tone.md", "", "memory/topic.md"],
)
def test_is_memory_only_is_the_exact_inverse_of_the_shared_predicate(destination: str) -> None:
    assert _is_memory_only(destination) is (not points_at_core_fix(destination))
