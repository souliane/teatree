"""The quote gate's operator-facing reasons — one renderer, one row per surface.

The #1401 arm's wording is correct and is ridden by the never-lockout contract, the
liveness corpus and the deny-circuit leak family, so it is frozen against its literal.
The #171 arm is the one #4381 corrected: it used to borrow the dispatch arm's sentence
and tell the operator their task-list ENTRY was a prompt a sub-agent would echo.
"""

import pytest

from teatree.hooks import quote_gate_messages as messages
from teatree.hooks.quote_scanner import HIGH, MEDIUM, Finding, ScanResult


def _high() -> ScanResult:
    return ScanResult(findings=[Finding(name="heading-user-mandate", severity=HIGH, excerpt="x")])


class TestThePublishArm:
    def test_block_message_lists_matched_pattern_names(self) -> None:
        message = messages.format_block_message(_high())
        assert "heading-user-mandate" in message
        # The escape names the env PREFIX that works on every command, never a
        # ``--quote-ok`` CLI flag a ``t3 review post-comment`` subcommand would
        # reject as an unknown option (#1415).
        assert "QUOTE_OK=1" in message
        assert "--quote-ok" not in message

    def test_warn_message_lists_matched_pattern_names(self) -> None:
        result = ScanResult(findings=[Finding(name="per-user-direction", severity=MEDIUM, excerpt="x")])
        assert "per-user-direction" in messages.format_warn_message(result)


class TestEachArmDescribesItsOwnSurface:
    def test_the_dispatch_arm_is_byte_identical_to_its_shipped_wording(self) -> None:
        assert messages.format_dispatch_block_message(_high()) == (
            "BLOCKED: pre-dispatch quote-scanner gate (#1401). The Agent/Task prompt carries verbatim "
            'user-voice/PII content (e.g. "x") — matched patterns: heading-user-mandate. Paraphrase it '
            "into author-voice description before dispatching (the sub-agent would otherwise echo it "
            "into a published output, defeating the #1213 publish gate). If the match is a false "
            "positive, add `[quote-ok: <reason>]` near the start of the prompt."
        )

    def test_the_task_entry_arm_names_the_carrier_it_actually_scans(self) -> None:
        message = messages.format_task_entry_block_message(_high())
        assert "task subject/description" in message
        assert "heading-user-mandate" in message
        assert "[quote-ok: <reason>]" in message

    @pytest.mark.parametrize("retired", ["pre-dispatch", "Agent/Task prompt", "before dispatching", "sub-agent"])
    def test_the_task_entry_arm_asserts_no_dispatch_premise(self, retired: str) -> None:
        # The event has ONE producer, the TaskCreate tool, so this arm never sees a
        # dispatch — a reason claiming otherwise sends the reader to the wrong remedy.
        assert retired not in messages.format_task_entry_block_message(_high())

    def test_a_high_match_with_no_excerpt_drops_the_example_clause(self) -> None:
        result = ScanResult(findings=[Finding(name="heading-user-mandate", severity=HIGH, excerpt="")])
        assert "(e.g." not in messages.format_task_entry_block_message(result)


class TestTheSurfacesAreDistinct:
    def test_no_two_surfaces_share_a_clause_set(self) -> None:
        # A surface added as a copy of another is the shape this table exists to stop.
        surfaces = [messages.DISPATCH_SURFACE, messages.TASK_ENTRY_SURFACE]
        assert len({tuple(vars(s).values()) for s in surfaces}) == len(surfaces)
