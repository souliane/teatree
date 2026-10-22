"""Every read-only phase's real system context fits the executor budget with production-sized skills."""

from django.test import TestCase

from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.read_only_venue_brief import READ_ONLY_DIRECTIVE, read_only_system_context
from tests.teatree_agents._read_only_review_world import STAND_IN_BYTES, read_only_context, read_only_review_world

_HEADROOM_BYTES = 4096
_PHASES = (
    ("planning", False, "--- SKILL: architecture-design ---"),
    ("reviewing", False, "--- SKILL: review ---"),
    ("reviewing", True, "--- SKILL: review ---"),
    ("critic_reviewing", False, "--- SKILL: review ---"),
)


class TestReadOnlyPhaseBudget(TestCase):
    def test_every_read_only_phase_keeps_headroom_and_required_guidance(self) -> None:
        sizes: dict[str, int] = {}
        with read_only_review_world():
            for phase, reviewer, lifecycle in _PHASES:
                context = read_only_system_context(read_only_context(phase, reviewer=reviewer))
                label = f"{phase}:{'reviewer' if reviewer else 'author'}"
                sizes[label] = len(context.encode())
                assert context.startswith("\n".join(READ_ONLY_DIRECTIVE)), label
                assert "--- RULES.md ---" in context, label
                assert lifecycle in context, label
                assert "summary" in context, label
                if phase == "planning":
                    assert all("x" * size in context for size in STAND_IN_BYTES.values() if size > 10_000), label
        assert all(size <= MAX_APPEND_BYTES - _HEADROOM_BYTES for size in sizes.values()), sizes
