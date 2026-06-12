"""Pre-publish privacy gate tests (#1295 capability J).

The gate is sibling to the close-keyword gate: it fires only on writes
to a repo in :attr:`OverlayConfig.public_repos`, scans the candidate
text for the overlay's redact-terms + the default quote-anchor
patterns, and refuses when any match fires.
"""

import pytest

from teatree.core.gates.privacy_gate import format_refusal, scan_for_publication

PUBLIC = "souliane/teatree"
PRIVATE = "private-org/internal-repo"
REDACT_ACRONYM = "ACME"
REDACT_PRIV_PATH = "private-org/internal-repo"


def test_public_target_blocked_on_redact_term() -> None:
    result = scan_for_publication(
        text=f"This touches {REDACT_ACRONYM} customer flow and references {REDACT_PRIV_PATH}.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=[REDACT_ACRONYM, REDACT_PRIV_PATH],
    )
    assert result.refused
    assert any(m.pattern_name.startswith("redact:") for m in result.matches)


def test_private_target_passes_same_content() -> None:
    result = scan_for_publication(
        text=f"This touches {REDACT_ACRONYM} customer flow and references {REDACT_PRIV_PATH}.",
        target_repo=PRIVATE,
        public_repos=[PUBLIC],
        redact_terms=[REDACT_ACRONYM, REDACT_PRIV_PATH],
    )
    assert not result.refused
    assert result.is_public is False


def test_bypass_flag_passes_public_target() -> None:
    result = scan_for_publication(
        text=f"Releases {REDACT_PRIV_PATH}#5 publicly.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=[REDACT_PRIV_PATH],
        bypass=True,
    )
    assert not result.refused


def test_blockquote_first_person_marker_blocked_on_public() -> None:
    result = scan_for_publication(
        text="From the customer (verbatim):\n\n> I said this and you should listen\n",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
    )
    assert result.refused
    pattern_names = {m.pattern_name for m in result.matches}
    assert any(name.startswith("blockquote_first_person") for name in pattern_names) or any(
        name == "verbatim_anchor" for name in pattern_names
    )


def test_no_match_on_clean_public_text() -> None:
    result = scan_for_publication(
        text="Refactor the loop scanner to read from the overlay config.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=[REDACT_ACRONYM],
    )
    assert not result.refused
    assert result.matches == ()


def test_redact_terms_skip_blank_entries() -> None:
    """Empty / None entries in redact_terms must not crash the scan."""
    result = scan_for_publication(
        text=f"Body references {REDACT_ACRONYM} once.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=["", REDACT_ACRONYM],
    )
    assert result.refused
    # Exactly one match — the empty term was skipped, not regex-matched.
    assert len(result.matches) == 1


def test_redact_term_substring_inside_a_word_is_not_flagged() -> None:
    """Fix #4: redact uses the SHARED whole-token matcher, not substring.

    A short redact term (here ``op``) must not surface inside a longer
    unbroken word (``cooperative``/``operation``) — the substring
    false-positive the old ``re.escape`` matching produced.
    """
    result = scan_for_publication(
        text="A cooperative operation by the operator.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=["op"],
    )
    assert not result.refused
    assert result.matches == ()


def test_redact_term_matches_whole_token_and_camelcase() -> None:
    """A redact term matches a whole token, incl. a camelCase/snake split."""
    result = scan_for_publication(
        text="Touches acme flow via acmeClient and acme_helper.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=["acme"],
    )
    assert result.refused
    assert all(m.pattern_name == "redact:acme" for m in result.matches)
    # The bare token, the camelCase split, and the snake split each match.
    assert len(result.matches) == 3


def test_custom_block_patterns_match() -> None:
    """Caller-supplied regexes are evaluated alongside the default set."""
    result = scan_for_publication(
        text="secret token ABC-12345 leaked here",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        block_patterns=[r"ABC-\d{5}"],
    )
    assert result.refused
    assert any(m.pattern_name.startswith("block:") for m in result.matches)


def test_invalid_regex_in_block_patterns_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed pattern must fail closed — block the publish and log, never silently pass."""
    import logging  # noqa: PLC0415

    with caplog.at_level(logging.WARNING, logger="teatree.core.gates.privacy_gate"):
        result = scan_for_publication(
            text="Body content",
            target_repo=PUBLIC,
            public_repos=[PUBLIC],
            block_patterns=["[unclosed", ""],
        )

    # Fail-closed: a rule that can't be evaluated blocks rather than passes.
    assert result.refused
    assert any(m.pattern_name.startswith("block:") for m in result.matches)
    assert "[unclosed" in caplog.text


def test_invalid_default_pattern_does_not_break_valid_matches() -> None:
    """A bad block pattern fails closed but valid patterns still surface their own matches."""
    result = scan_for_publication(
        text="secret token ABC-12345 leaked here",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        block_patterns=[r"ABC-\d{5}", "(unbalanced"],
    )
    assert result.refused
    names = {m.pattern_name for m in result.matches}
    # Both the valid match and the fail-closed bad-pattern marker are present.
    assert any(n.startswith("block:ABC") for n in names)
    assert any("unbalanced" in n for n in names)


def test_format_refusal_renders_matches_block() -> None:
    """The structured error message names the repo, the count, and each pattern."""
    result = scan_for_publication(
        text=f"Mentions {REDACT_ACRONYM} once.",
        target_repo=PUBLIC,
        public_repos=[PUBLIC],
        redact_terms=[REDACT_ACRONYM],
    )
    rendered = format_refusal(result)
    assert PUBLIC in rendered
    assert "privacy gate refused" in rendered
    assert REDACT_ACRONYM in rendered
    assert "--privacy-ok" in rendered
