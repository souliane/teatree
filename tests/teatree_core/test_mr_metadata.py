"""Tests for the deterministic MR title/description convention gate (#1540).

Pure-logic unit coverage of ``validate_mr_metadata`` — the helper that
``OverlayMetadata.validate_pr`` delegates to. It rejects a title that does not
match the overlay's ``mr_title_regex`` and a description that is empty or
carries no What/Why header, returning the EXACT expected format in each error.
"""

import pytest

from teatree.core.mr_metadata import DEFAULT_MR_TITLE_REGEX, expected_title_format, validate_mr_metadata

_GOOD_DESC = "## What\nAdds the gate.\n\n## Why\nThe convention is missed often."


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
        assert validate_mr_metadata("feat: ok", _GOOD_DESC, custom) == []
        rejected = validate_mr_metadata("chore: not allowed here", _GOOD_DESC, custom)
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
            "## What\nthe change",
            "## Why\nthe reason",
            "What: the change\nWhy: the reason",
            "Some preamble.\n\n## What\nbody",
        ],
    )
    def test_description_with_what_or_why_header_passes(self, description: str) -> None:
        assert validate_mr_metadata("feat(ship): ok", description, DEFAULT_MR_TITLE_REGEX) == []

    def test_description_without_what_why_rejected(self) -> None:
        errors = validate_mr_metadata(
            "feat(ship): ok", "Just a plain paragraph with no headers.", DEFAULT_MR_TITLE_REGEX
        )
        assert any("What" in err and "Why" in err for err in errors)


def test_both_failures_surface_together() -> None:
    errors = validate_mr_metadata("bad title", "no headers here", DEFAULT_MR_TITLE_REGEX)
    assert len(errors) == 2
