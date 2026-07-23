"""``redact_for_local_display`` masks leak-gate matches for LOCAL display (#3673).

The one redactor a local viewer (the dashboard transcript panel) routes through:
it reuses the publication scan's vocabulary + matcher and blanks each matched
span with ``LEAK_MASK`` — no second redactor. It never raises on an unresolvable
vocabulary; it degrades to fewer masks.
"""

import pytest

from teatree.core.gates import privacy_gate
from teatree.core.gates.privacy_gate import LEAK_MASK, redact_for_local_display


@pytest.fixture
def set_rules(monkeypatch: pytest.MonkeyPatch):
    def _set(*, redact: list[str] | None = None, block: list[str] | None = None, banned=()) -> None:
        monkeypatch.setattr(privacy_gate, "_overlay_privacy_rules", lambda: (redact or [], block or []))
        monkeypatch.setattr(privacy_gate, "_db_banned_terms", lambda: tuple(banned))

    return _set


def test_masks_a_redact_term(set_rules) -> None:
    set_rules(redact=["SECRETCORP"])
    out = redact_for_local_display("the SECRETCORP account leaked")
    assert "SECRETCORP" not in out
    assert LEAK_MASK in out


def test_masks_a_banned_term(set_rules) -> None:
    set_rules(banned=["ACMECODENAME"])
    out = redact_for_local_display("mentions ACMECODENAME once")
    assert "ACMECODENAME" not in out
    assert LEAK_MASK in out


def test_masks_a_block_pattern(set_rules) -> None:
    set_rules(block=[r"sk-[a-z0-9]{6}"])
    out = redact_for_local_display("token sk-abc123 here")
    assert "sk-abc123" not in out
    assert LEAK_MASK in out


def test_clean_text_is_unchanged(set_rules) -> None:
    set_rules(redact=["SECRETCORP"])
    assert redact_for_local_display("nothing to hide") == "nothing to hide"


def test_overlapping_spans_do_not_corrupt(set_rules) -> None:
    # two redact terms whose matches overlap must merge, not double-replace.
    set_rules(redact=["alpha", "alphabet"])
    out = redact_for_local_display("the alphabet soup")
    assert "alpha" not in out
    assert LEAK_MASK in out
    assert "soup" in out


def test_overlapping_matches_merge_into_one_mask(set_rules) -> None:
    # Two block patterns whose matches overlap must MERGE into a single span
    # (the right-to-left merge in `_mask_spans`), never emit two masks or corrupt
    # the surrounding text.
    set_rules(block=[r"abcd", r"cdef"])
    out = redact_for_local_display("x abcdef y")
    assert "abcd" not in out
    assert "cdef" not in out
    assert out.count(LEAK_MASK) == 1
    assert out == f"x {LEAK_MASK} y"


def test_unresolvable_vocab_does_not_raise(set_rules) -> None:
    set_rules(redact=None, block=None, banned=())
    # built-in quote anchors still apply; no overlay/banned terms → clean text passes.
    assert redact_for_local_display("plain text") == "plain text"
