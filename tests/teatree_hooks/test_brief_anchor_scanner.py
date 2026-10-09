"""Tests for the dispatch-brief anchor detector (issue #4341).

Orchestrator-written briefs assert specifics — ``file:line``, config keys, counts,
"X does not exist" — that are routinely stale. The countermeasure that caught every
one of ~23 disconfirmed premises across two sessions was one clause telling the
sub-agent it may overrule the brief. The detector fires only when a brief BOTH
asserts verifiable specifics AND carries neither a SHA anchor nor that clause.

The load-bearing half is the NO-FIRE corpus: this runs on every dispatch the fleet
makes, so a matcher that fires on ordinary prose is noise the reader learns to skip.
"""

import pytest

from teatree.hooks import brief_anchor_scanner as scanner

_CLAUSE = "If the review or this brief contradicts the code, TRUST THE CODE and say I was wrong."


class TestAssertionTriggers:
    @pytest.mark.parametrize(
        ("kind", "brief"),
        [
            ("file:line", "The bug is in src/teatree/core/ticket.py:412 — fix the guard there."),
            ("commit SHA", "Cold-review the branch; the baseline is b6a1ed72a and nothing has moved."),
            ("count", "There are 3 callers of the helper; update each one."),
            ("non-existence", "The overlay hook does not exist yet, so add it from scratch."),
            ("config key", "Read the ceiling from PYTEST_XDIST_AUTO_NUM_WORKERS before dispatching."),
        ],
    )
    def test_an_unanchored_specific_fires(self, kind: str, brief: str) -> None:
        verdict = scanner.find_unanchored_assertions(brief)
        assert verdict is not None, f"{kind} assertion should fire when unanchored"
        assert kind in verdict.kinds

    def test_the_verdict_quotes_the_assertion_it_saw(self) -> None:
        verdict = scanner.find_unanchored_assertions("The guard lives at src/teatree/core/ticket.py:412.")
        assert verdict is not None
        assert "src/teatree/core/ticket.py:412" in " ".join(verdict.samples)


class TestAnchorsClear:
    def test_a_sha_anchor_clears(self) -> None:
        brief = "Verified at b6a1ed72a: the guard is in src/teatree/core/ticket.py:412 and there are 3 callers."
        assert scanner.find_unanchored_assertions(brief) is None

    @pytest.mark.parametrize(
        "clause",
        [
            _CLAUSE,
            "If this brief contradicts the code, trust the code.",
            "The brief may be stale — the code wins.",
            "Treat every fact in this brief as a claim to verify, not a given.",
            "If I got this wrong, say so and follow what the code actually does.",
        ],
    )
    def test_a_trust_the_code_clause_clears(self, clause: str) -> None:
        brief = f"The guard is at src/teatree/core/ticket.py:412 and TEATREE_HOME is unset. {clause}"
        assert scanner.find_unanchored_assertions(brief) is None


class TestNoFireCorpus:
    @pytest.mark.parametrize(
        "brief",
        [
            "",
            "   \n  ",
            "Implement the feature described on the ticket and open a PR when the lane is green.",
            "NEVER add an AI signature. ALWAYS run the affected lane. IMPORTANT: commit before finishing.",
            "Review the open PR and report a verdict of merge_safe or hold, with reasons.",
            "Summarise what changed on the branch and post the summary back in your result envelope.",
        ],
    )
    def test_a_brief_asserting_no_specifics_never_fires(self, brief: str) -> None:
        assert scanner.find_unanchored_assertions(brief) is None

    @pytest.mark.parametrize("word", ["defaced", "decade", "effaced", "facade"])
    def test_a_hex_alphabet_english_word_is_not_a_sha(self, word: str) -> None:
        assert scanner.find_unanchored_assertions(f"The old behaviour was {word} by the rewrite.") is None

    def test_a_non_string_payload_is_silent(self) -> None:
        assert scanner.find_unanchored_assertions(None) is None


class TestWarningText:
    def test_the_warning_quotes_the_canonical_remedy_clause(self) -> None:
        verdict = scanner.find_unanchored_assertions("The guard is at src/teatree/core/ticket.py:412.")
        assert verdict is not None
        assert _CLAUSE in scanner.format_warning(verdict)

    def test_the_warning_names_what_fired(self) -> None:
        verdict = scanner.find_unanchored_assertions("There are 3 callers of src/teatree/core/ticket.py:412.")
        assert verdict is not None
        warning = scanner.format_warning(verdict)
        assert "file:line" in warning
        assert "count" in warning
