"""Dispatch-time sub-agent typing check (PR-12): t3-<type>-<id> display names."""

from teatree.loop.dispatch_gates import GENERAL_PURPOSE_SUBAGENT, spawn_display_name


class TestSpawnDisplayName:
    def test_type_prefixed_name_from_namespaced_subagent(self) -> None:
        assert spawn_display_name("t3:coder", 42) == "t3-coder-42"
        assert spawn_display_name("t3:reviewer", 7) == "t3-reviewer-7"
        assert spawn_display_name("t3:review-request", 9) == "t3-review-request-9"

    def test_empty_subagent_degrades_to_general_purpose_marker(self) -> None:
        assert spawn_display_name("", 3) == f"t3-{GENERAL_PURPOSE_SUBAGENT}-3"
        assert spawn_display_name("   ", 3) == f"t3-{GENERAL_PURPOSE_SUBAGENT}-3"
