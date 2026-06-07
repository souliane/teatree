"""Tests for the deterministic MR title/description convention gate (#1540, #1367).

Pure-logic unit coverage of ``validate_mr_metadata`` — the helper that
``OverlayMetadata.validate_pr`` delegates to. It rejects a title that does not
match the overlay's ``mr_title_regex``, a description whose first line is not
conventional-commit (the GitLab CI gate's own rule, #1367), and a description
that is empty or carries no What/Why header, returning the EXACT expected
format in each error.
"""

import pytest

from teatree.core.mr_metadata import DEFAULT_MR_TITLE_REGEX, expected_title_format, validate_mr_metadata

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
