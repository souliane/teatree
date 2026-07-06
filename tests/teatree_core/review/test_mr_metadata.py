"""Tests for the deterministic MR title/description convention gate (#1540, #1367).

Pure-logic unit coverage of ``validate_mr_metadata`` — the helper that
``OverlayMetadata.validate_pr`` delegates to. It rejects a title that does not
match the overlay's ``mr_title_regex``, a description whose first line is not
conventional-commit (the GitLab CI gate's own rule, #1367), and a description
that is empty or carries no What/Why header, returning the EXACT expected
format in each error.
"""

import pytest

from teatree.core.review.mr_metadata import (
    DEFAULT_MR_TITLE_REGEX,
    ensure_standard_body,
    expected_title_format,
    missing_required_sections,
    validate_mr_metadata,
)

_GOOD_DESC = "feat(ship): add the gate (#1540)\n\n## What\nAdds the gate.\n\n## Why\nThe convention is missed often."


class TestTitleRegex:
    @pytest.mark.parametrize(
        "title",
        [
            "feat(ship): add the gate (#1540)",
            "fix: correct the off-by-one",
            "chore(deps): bump uv",
            "docs: clarify the readme",
            "refactor(core): split the module",
            "test: cover the edge case",
            "perf: cache the lookup",
            "build: pin the wheel",
            "ci: add the job",
            "improvement: enhance the resolver",
            "config(env): update the variables",
            "techdebt(pricing): regression guards for the rate clamp",
            "feat(auth): normal",
            "feat!: drop legacy API",
            "feat(auth)!: rework login",
        ],
    )
    def test_conforming_title_passes(self, title: str) -> None:
        assert validate_mr_metadata(title, _GOOD_DESC, DEFAULT_MR_TITLE_REGEX) == []

    @pytest.mark.parametrize(
        "title",
        [
            "Add the gate",
            "Feat: capitalised type",
            "feature: not in the type set",
            "",
            "feat(ship):no-space-after-colon",
        ],
    )
    def test_non_conforming_title_rejected(self, title: str) -> None:
        errors = validate_mr_metadata(title, _GOOD_DESC, DEFAULT_MR_TITLE_REGEX)
        assert errors
        assert any(expected_title_format(DEFAULT_MR_TITLE_REGEX) in err for err in errors)

    def test_per_overlay_regex_is_honoured(self) -> None:
        custom = r"^(feat|fix): .+"
        good = "feat: add the gate\n\n## What\nbody\n\n## Why\nreason"
        assert validate_mr_metadata("feat: ok", good, custom) == []
        rejected = validate_mr_metadata("chore: not allowed here", good, custom)
        assert rejected
        assert any(custom in err for err in rejected)


class TestDescriptionWhatWhy:
    def test_empty_description_rejected(self) -> None:
        errors = validate_mr_metadata("feat(ship): ok", "", DEFAULT_MR_TITLE_REGEX)
        assert any("description" in err.lower() for err in errors)

    def test_whitespace_only_description_rejected(self) -> None:
        errors = validate_mr_metadata("feat(ship): ok", "   \n\t  ", DEFAULT_MR_TITLE_REGEX)
        assert any("description" in err.lower() for err in errors)

    @pytest.mark.parametrize(
        "description",
        [
            "feat(ship): ok\n\n## What\nthe change",
            "feat(ship): ok\n\n## Why\nthe reason",
            "feat(ship): ok\n\nWhat: the change\nWhy: the reason",
            "feat(ship): ok\n\nSome preamble.\n\n## What\nbody",
        ],
    )
    def test_description_with_what_or_why_header_passes(self, description: str) -> None:
        assert validate_mr_metadata("feat(ship): ok", description, DEFAULT_MR_TITLE_REGEX) == []

    def test_description_without_what_why_rejected(self) -> None:
        errors = validate_mr_metadata(
            "feat(ship): ok", "feat(ship): ok\n\nJust a plain paragraph with no headers.", DEFAULT_MR_TITLE_REGEX
        )
        assert any("What" in err and "Why" in err for err in errors)


class TestDescriptionFirstLineConventionalCommit:
    """The description's FIRST LINE must be conventional-commit (#1367).

    The GitLab ``validate_mr_title_and_description`` CI gate parses the
    LITERAL first line of the description and rejects anything not in
    conventional-commit form — it does NOT fall back to the MR title. A
    description starting with ``## Summary`` / ``## What`` passes the title
    and What/Why checks yet still reds the pipeline. The client-side gate
    must encode the SAME first-line rule so the validator round-trip is
    eliminated.
    """

    @pytest.mark.parametrize(
        "description",
        [
            "## Summary\nAdds the gate.\n\n## Why\nThe convention is missed often.",
            "## What\nthe change\n\n## Why\nthe reason",
            "What: the change\nWhy: the reason",
            "Some preamble.\n\n## What\nbody",
        ],
    )
    def test_non_conventional_first_line_rejected(self, description: str) -> None:
        errors = validate_mr_metadata("feat(ship): ok", description, DEFAULT_MR_TITLE_REGEX)
        assert any("first line" in err.lower() for err in errors)

    @pytest.mark.parametrize(
        "description",
        [
            "feat(ship): add the gate (#1367)\n\n## What\nthe change\n\n## Why\nthe reason",
            "fix: correct the off-by-one\n\n## What\nbody",
        ],
    )
    def test_conventional_first_line_passes(self, description: str) -> None:
        assert validate_mr_metadata("feat(ship): ok", description, DEFAULT_MR_TITLE_REGEX) == []

    def test_first_line_must_match_overlay_regex(self) -> None:
        custom = r"^(feat|fix): .+"
        rejected = validate_mr_metadata("feat: ok", "chore: nope\n\n## What\nbody", custom)
        assert any("first line" in err.lower() for err in rejected)


def test_all_failures_surface_together() -> None:
    errors = validate_mr_metadata("bad title", "no headers here", DEFAULT_MR_TITLE_REGEX)
    assert len(errors) == 3
    assert any("title" in err.lower() and "first line" not in err.lower() for err in errors)
    assert any("first line" in err.lower() for err in errors)
    assert any("What" in err and "Why" in err for err in errors)


_GOOD_DESC_WITH_CONFIG = (
    "feat(ship): add the gate (#312)\n\n"
    "## What\nAdds the gate.\n\n"
    "## Why\nThe convention is missed often.\n\n"
    "## Configuration\nThis MR does not need configuration and will be applied automatically once merged."
)


class TestMissingRequiredSections:
    """An overlay declares mandatory description sections (e.g. ``Configuration``).

    ``missing_required_sections`` returns the declared section headers that are
    absent from the description, matched case-insensitively against an
    ``## Header`` / ``# Header`` markdown header anywhere in the body. The gate
    surfaces every missing one so a reviewer can tell "no config needed" from
    "the author forgot the section".
    """

    def test_no_required_sections_means_nothing_missing(self) -> None:
        assert missing_required_sections(_GOOD_DESC, []) == []

    def test_present_section_is_not_flagged(self) -> None:
        assert missing_required_sections(_GOOD_DESC_WITH_CONFIG, ["Configuration"]) == []

    def test_absent_section_is_flagged(self) -> None:
        assert missing_required_sections(_GOOD_DESC, ["Configuration"]) == ["Configuration"]

    def test_section_match_is_case_insensitive(self) -> None:
        desc = "feat: ok\n\n## What\nbody\n\n## configuration\nnothing to do"
        assert missing_required_sections(desc, ["Configuration"]) == []

    def test_section_must_be_a_markdown_header_not_inline_mention(self) -> None:
        # A bare prose mention of the word is NOT the section — only a header counts.
        desc = "feat: ok\n\n## What\nThe configuration is unchanged.\n\n## Why\nreason"
        assert missing_required_sections(desc, ["Configuration"]) == ["Configuration"]

    def test_multiple_missing_sections_all_surface(self) -> None:
        assert missing_required_sections(_GOOD_DESC, ["Configuration", "Rollout"]) == [
            "Configuration",
            "Rollout",
        ]


class TestValidateMrMetadataRequiredSections:
    """``validate_mr_metadata`` flags a description missing a required section."""

    def test_missing_required_section_is_a_violation(self) -> None:
        errors = validate_mr_metadata(
            "feat(ship): ok",
            _GOOD_DESC,
            DEFAULT_MR_TITLE_REGEX,
            required_sections=["Configuration"],
        )
        assert any("Configuration" in err for err in errors)

    def test_present_required_section_passes(self) -> None:
        errors = validate_mr_metadata(
            "feat(ship): add the gate (#312)",
            _GOOD_DESC_WITH_CONFIG,
            DEFAULT_MR_TITLE_REGEX,
            required_sections=["Configuration"],
        )
        assert errors == []

    def test_required_sections_default_to_none(self) -> None:
        # Backward-compatible: no required_sections arg → no section enforcement.
        assert validate_mr_metadata("feat(ship): ok", _GOOD_DESC, DEFAULT_MR_TITLE_REGEX) == []


class TestEnsureStandardBody:
    """The generator emits ``## What`` / ``## Why`` + declared sections by default.

    ``ensure_standard_body`` takes a description (built from the title + commit
    body) and APPENDS any standard or required section it is missing — so a
    thin commit body still ships a description carrying every required header.
    An already-present section is never duplicated.
    """

    def test_thin_body_gets_standard_sections_appended(self) -> None:
        out = ensure_standard_body("feat(ship): ok", required_sections=["Configuration"])
        assert "## What" in out
        assert "## Why" in out
        assert "## Configuration" in out
        # First line preserved (release-notes divergence guard).
        assert out.splitlines()[0] == "feat(ship): ok"

    def test_existing_sections_are_not_duplicated(self) -> None:
        rich = _GOOD_DESC_WITH_CONFIG
        out = ensure_standard_body(rich, required_sections=["Configuration"])
        assert out.count("## What") == 1
        assert out.count("## Why") == 1
        assert out.count("## Configuration") == 1

    def test_required_section_appended_when_what_why_present(self) -> None:
        out = ensure_standard_body(_GOOD_DESC, required_sections=["Configuration"])
        assert out.count("## What") == 1
        assert out.count("## Why") == 1
        assert "## Configuration" in out

    def test_output_passes_the_gate(self) -> None:
        out = ensure_standard_body("feat(ship): add the gate (#312)", required_sections=["Configuration"])
        errors = validate_mr_metadata(
            "feat(ship): add the gate (#312)",
            out,
            DEFAULT_MR_TITLE_REGEX,
            required_sections=["Configuration"],
        )
        assert errors == []

    def test_section_default_body_is_emitted_under_a_missing_section(self) -> None:
        no_config = "This MR does not need configuration and will be applied automatically once merged."
        out = ensure_standard_body(
            "feat(ship): add the gate (#312)",
            required_sections=["Configuration"],
            section_defaults={"Configuration": no_config},
        )
        assert f"## Configuration\n{no_config}" in out

    def test_section_default_key_match_is_case_insensitive(self) -> None:
        out = ensure_standard_body(
            "feat(ship): ok",
            required_sections=["Configuration"],
            section_defaults={"configuration": "default text"},
        )
        assert "## Configuration\ndefault text" in out

    def test_section_default_not_re_applied_when_section_present(self) -> None:
        # Section already in the body -> never re-rendered, default ignored.
        out = ensure_standard_body(
            _GOOD_DESC_WITH_CONFIG,
            required_sections=["Configuration"],
            section_defaults={"Configuration": "SHOULD NOT APPEAR"},
        )
        assert "SHOULD NOT APPEAR" not in out
