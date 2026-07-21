"""Leaf constants + binding heuristic shared across the dream weight ladder (F6.11).

The weight ladder and the "is this BINDING doctrine?" heuristic were copied per
module (engine / merge / decay). These tests pin the ONE shared copy and prove the
three consumers read it, so the ladder and the binding rule can never drift apart.
"""

from django.test import SimpleTestCase

from teatree.loops.dream import _shared


class IsBindingTextTestCase(SimpleTestCase):
    def test_binding_marker_is_detected_case_insensitively(self) -> None:
        assert _shared.is_binding_text("this is a BINDING rule")
        assert _shared.is_binding_text("a binding directive")

    def test_non_negotiable_clause_is_binding(self) -> None:
        # Non-Negotiable doctrine is load-bearing user doctrine — it counts as binding.
        assert _shared.is_binding_text("a Non-Negotiable directive")

    def test_ordinary_text_is_not_binding(self) -> None:
        assert not _shared.is_binding_text("an ordinary lesson with no doctrine")


class WeightLadderTestCase(SimpleTestCase):
    def test_ladder_is_monotonically_ranked(self) -> None:
        # Highest signal first: binding > feedback > correction > retro > cold-review >
        # deny-streak > other. The single source both engine and merge order by.
        ladder = [
            _shared.WEIGHT_BINDING,
            _shared.WEIGHT_FEEDBACK,
            _shared.WEIGHT_CORRECTION,
            _shared.WEIGHT_RETRO,
            _shared.WEIGHT_COLD_REVIEW,
            _shared.WEIGHT_DENY_STREAK,
            _shared.WEIGHT_OTHER,
        ]
        assert ladder == sorted(ladder, reverse=True)
        assert len(set(ladder)) == len(ladder)  # every floor distinct

    def test_engine_and_merge_read_the_same_ladder(self) -> None:
        # The consumers import the shared floors under their local aliases, so a change
        # to the leaf reaches both — no per-module copy to drift.
        from teatree.loops.dream import engine, merge  # noqa: PLC0415

        assert engine._WEIGHT_BINDING == _shared.WEIGHT_BINDING
        assert engine._WEIGHT_OTHER == _shared.WEIGHT_OTHER
        assert merge._WEIGHT_BINDING == _shared.WEIGHT_BINDING
        assert merge._WEIGHT_FEEDBACK == _shared.WEIGHT_FEEDBACK
