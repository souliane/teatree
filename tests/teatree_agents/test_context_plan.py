"""Context-assembly + prompt-cache control API (#3157 E2).

Acceptance: the rendered cacheable head is byte-identical across two dispatches for the same
repo (no timestamps/uuids before the last breakpoint), and the ``cache`` boundaries map to
``cache_control`` breakpoints (≤4) with a TTL for the direct Anthropic binding.
"""

import pytest

from teatree.agents.context_plan import (
    MAX_CACHE_BREAKPOINTS,
    CacheBreakpoint,
    ContextPlan,
    ContextSegment,
    SegmentStability,
    UnstableCacheHeadError,
    assert_byte_stable_head,
    cache_control_plan,
    find_unstable_tokens,
)


def _repo_plan(*, task_id: int) -> ContextPlan:
    """A plan for one repo whose cacheable head is byte-stable across task ids."""
    return ContextPlan.ordered(
        [
            ContextSegment("teatree conventions preamble", SegmentStability.STATIC),
            ContextSegment("repo digest: schemas + module map", SegmentStability.PER_REPO, cache=True, ttl="1h"),
            ContextSegment(f"Task ID: {task_id}\ncurrent live diff", SegmentStability.PER_TASK),
            ContextSegment("2026-07-11T09:00 live tail", SegmentStability.VOLATILE),
        ]
    )


class TestOrderingAndRendering:
    def test_ordered_sorts_by_stability_rank(self) -> None:
        plan = ContextPlan.ordered(
            [
                ContextSegment("volatile", SegmentStability.VOLATILE),
                ContextSegment("static", SegmentStability.STATIC),
                ContextSegment("per_task", SegmentStability.PER_TASK),
                ContextSegment("per_repo", SegmentStability.PER_REPO),
            ]
        )
        assert [seg.content for seg in plan.segments] == ["static", "per_repo", "per_task", "volatile"]

    def test_render_joins_every_segment_in_order(self) -> None:
        plan = _repo_plan(task_id=1)
        rendered = plan.render()
        assert rendered.startswith("teatree conventions preamble")
        assert "live tail" in rendered


class TestByteStableHead:
    def test_cacheable_head_is_byte_identical_across_dispatches_for_the_same_repo(self) -> None:
        head_a = _repo_plan(task_id=1).cacheable_head()
        head_b = _repo_plan(task_id=999).cacheable_head()
        assert head_a == head_b
        assert "Task ID" not in head_a  # per-task content is strictly after the breakpoint

    def test_stable_head_carries_no_timestamps_or_uuids(self) -> None:
        plan = _repo_plan(task_id=1)
        assert find_unstable_tokens(plan.cacheable_head()) == []
        assert_byte_stable_head(plan)  # does not raise

    def test_a_timestamp_before_the_breakpoint_is_rejected(self) -> None:
        plan = ContextPlan.ordered(
            [
                ContextSegment("built at 2026-07-11T09:00:00", SegmentStability.STATIC),
                ContextSegment("repo digest", SegmentStability.PER_REPO, cache=True),
            ]
        )
        assert find_unstable_tokens(plan.cacheable_head())
        with pytest.raises(UnstableCacheHeadError):
            assert_byte_stable_head(plan)

    def test_a_uuid_before_the_breakpoint_is_rejected(self) -> None:
        plan = ContextPlan.ordered(
            [
                ContextSegment("run 550e8400-e29b-41d4-a716-446655440000", SegmentStability.PER_REPO, cache=True),
            ]
        )
        with pytest.raises(UnstableCacheHeadError):
            assert_byte_stable_head(plan)

    def test_volatile_tail_may_carry_timestamps(self) -> None:
        # A plan whose only breakpoint precedes the volatile timestamp passes — the timestamp
        # is after the head.
        plan = _repo_plan(task_id=1)
        assert_byte_stable_head(plan)
        assert "2026-07-11T09:00" in plan.render()

    def test_no_breakpoint_means_empty_head_always_stable(self) -> None:
        plan = ContextPlan.ordered([ContextSegment("built 2026-07-11T09:00", SegmentStability.STATIC)])
        assert plan.cacheable_head() == ""
        assert_byte_stable_head(plan)


class TestCacheBreakpoints:
    def test_maps_cache_boundaries_to_breakpoints_with_ttl(self) -> None:
        plan = _repo_plan(task_id=1)
        breakpoints = cache_control_plan(plan)
        assert breakpoints == (CacheBreakpoint(segment_index=1, ttl="1h"),)

    def test_at_most_four_breakpoints_kept_deepest_wins(self) -> None:
        segments = [ContextSegment(f"s{i}", SegmentStability.PER_REPO, cache=True) for i in range(6)]
        plan = ContextPlan(segments=tuple(segments))
        breakpoints = plan.cache_breakpoints()
        assert len(breakpoints) == MAX_CACHE_BREAKPOINTS
        # The LAST four (deepest prefix) win.
        assert [bp.segment_index for bp in breakpoints] == [2, 3, 4, 5]

    def test_cacheable_head_spans_up_to_the_last_breakpoint(self) -> None:
        plan = ContextPlan.ordered(
            [
                ContextSegment("a", SegmentStability.STATIC, cache=True),
                ContextSegment("b", SegmentStability.PER_REPO, cache=True),
                ContextSegment("c", SegmentStability.PER_TASK),
            ]
        )
        assert plan.cacheable_head() == "a\nb"


class TestSegmentValidation:
    def test_a_cache_breakpoint_on_a_volatile_segment_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cache breakpoint"):
            ContextSegment("x", SegmentStability.VOLATILE, cache=True)

    def test_a_cache_breakpoint_on_a_per_task_segment_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cache breakpoint"):
            ContextSegment("x", SegmentStability.PER_TASK, cache=True)

    def test_an_invalid_ttl_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="TTL"):
            ContextSegment("x", SegmentStability.STATIC, cache=True, ttl="10m")
