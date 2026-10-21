"""The production-sized read-only phase contexts fit with real skill roots."""

from django.test import TestCase

from teatree.agents.context_budget import MAX_APPEND_BYTES
from teatree.agents.read_only_venue_brief import READ_ONLY_DIRECTIVE
from tests.teatree_agents._read_only_review_world import read_only_context, read_only_review_world


class TestReadOnlyPhaseBudget(TestCase):
    def test_every_read_only_phase_keeps_headroom_and_required_guidance(self) -> None:
        sizes: dict[str, int] = {}
        with read_only_review_world():
            for phase, reviewer in (
                ("planning", False),
                ("reviewing", False),
                ("reviewing", True),
                ("critic_reviewing", False),
            ):
                context = read_only_context(phase, reviewer=reviewer)
                label = f"{phase}:{'reviewer' if reviewer else 'author'}"
                sizes[label] = len(context.encode())
                assert context.startswith("\n".join(READ_ONLY_DIRECTIVE))
                assert "--- RULES.md ---" in context
                assert "summary" in context
        assert all(size <= MAX_APPEND_BYTES - 4096 for size in sizes.values()), sizes
