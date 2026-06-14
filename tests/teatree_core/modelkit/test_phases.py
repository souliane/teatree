"""Phase-name normalization — single vocabulary across skills and FSM (#694)."""

import pytest

from teatree.core.modelkit.phases import normalize_phase, phase_transition


class TestNormalizePhase:
    @pytest.mark.parametrize(
        ("raw", "canonical"),
        [
            ("scope", "scoping"),
            ("scoping", "scoping"),
            ("code", "coding"),
            ("coding", "coding"),
            ("test", "testing"),
            ("testing", "testing"),
            ("review", "reviewing"),
            ("reviewing", "reviewing"),
            ("ship", "shipping"),
            ("shipping", "shipping"),
            ("retro", "retro"),
            ("retrospect", "retro"),
            ("retrospecting", "retro"),
            ("request_review", "requesting_review"),
            ("requesting_review", "requesting_review"),
        ],
    )
    def test_short_and_gerund_forms_map_to_one_canonical(self, raw: str, canonical: str) -> None:
        assert normalize_phase(raw) == canonical

    def test_case_and_whitespace_insensitive(self) -> None:
        assert normalize_phase("  Review ") == "reviewing"

    def test_unknown_phase_returns_input_lowered_and_stripped(self) -> None:
        # Unknown phases pass through (so visiting a free-form phase still
        # records something) but never crash.
        assert normalize_phase(" Custom ") == "custom"


class TestPhaseTransition:
    @pytest.mark.parametrize(
        ("phase", "transition"),
        [
            ("scope", "scope"),
            ("scoping", "scope"),
            ("code", "code"),
            ("coding", "code"),
            ("test", "test"),
            ("testing", "test"),
            ("review", "review"),
            ("reviewing", "review"),
            ("ship", "ship"),
            ("shipping", "ship"),
            ("retro", "retrospect"),
            ("retrospecting", "retrospect"),
            ("request_review", "request_review"),
        ],
    )
    def test_both_vocabularies_resolve_to_fsm_transition(self, phase: str, transition: str) -> None:
        assert phase_transition(phase) == transition

    def test_phase_with_no_transition_returns_none(self) -> None:
        assert phase_transition("custom") is None
