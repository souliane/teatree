"""The cold phase vocabulary also drives review write authority."""

from teatree.config.phase_names import REVIEW_PHASES, normalize_phase, phase_spellings


def test_review_aliases_and_retiring_phase_remain_distinct() -> None:
    assert normalize_phase(" ReViEw ") == "reviewing"
    assert normalize_phase("request-review") == "requesting_review"
    assert "review" in phase_spellings("reviewing")
    assert "reviewing" in REVIEW_PHASES
    assert "requesting_review" not in REVIEW_PHASES
