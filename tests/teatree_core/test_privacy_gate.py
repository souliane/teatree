"""Pre-publish privacy gate tests (#1295 capability J).

The gate is sibling to the close-keyword gate: it fires only on writes
to a repo in :attr:`OverlayConfig.public_repos`, scans the candidate
text for the overlay's redact-terms + the default quote-anchor
patterns, and refuses when any match fires.
"""

from teatree.core.privacy_gate import scan_for_publication

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
