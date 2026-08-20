"""Tests for the deterministic MR title/description convention gate (#1540, #1367).

Pure-logic unit coverage of ``validate_mr_metadata`` — the helper that
``OverlayMetadata.validate_pr`` delegates to. It rejects a title that does not
match the overlay's ``mr_title_regex``, a description whose first line is not
conventional-commit (the GitLab CI gate's own rule, #1367), and a description
that is empty or carries no What/Why header, returning the EXACT expected
format in each error.
"""

import re

import pytest

from teatree.core.review.mr_metadata import (
    AUTO_CREATED_MARKER,
    DEFAULT_MR_TITLE_REGEX,
    auto_created_description,
    ensure_standard_body,
    expected_title_format,
    lacks_rationale,
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


class TestAutoCreatedDescription:
    """The body a PR opened with no author-written description ships.

    The no-orphan pre-push hook has only the commit to work from, so the body it
    renders must satisfy this same gate — and its ``## Why`` must state provenance
    rather than invent a rationale or issue an instruction nobody will follow.
    """

    _TITLE = "fix(pr): render a gate-conforming auto-created body"
    _ISSUE = "https://github.com/souliane/teatree/issues/4424"

    def _why(self, **kwargs: str) -> str:
        return auto_created_description(self._TITLE, "- the commit body", **kwargs).split("## Why\n", 1)[1]

    def test_output_passes_the_gate(self) -> None:
        out = auto_created_description(self._TITLE, "- the commit body")
        assert validate_mr_metadata(self._TITLE, out, DEFAULT_MR_TITLE_REGEX) == []

    def test_raw_commit_message_is_still_rejected(self) -> None:
        """Control: the gate is unchanged — only the generated body was fixed."""
        raw = f"{self._TITLE}\n\n- the commit body"
        assert validate_mr_metadata(self._TITLE, raw, DEFAULT_MR_TITLE_REGEX) != []

    def test_first_line_is_the_title(self) -> None:
        out = auto_created_description(self._TITLE, "- the commit body")
        assert out.splitlines()[0] == self._TITLE

    def test_what_carries_the_commit_body(self) -> None:
        out = auto_created_description(self._TITLE, "- the commit body")
        assert "## What\n- the commit body" in out

    def test_bodyless_commit_falls_back_to_the_title_under_what(self) -> None:
        out = auto_created_description(self._TITLE, "")
        assert f"## What\n{self._TITLE}" in out

    def test_why_issues_no_instruction_nobody_will_follow(self) -> None:
        """#4424: the hook opens the PR BECAUSE no author did, so an ask-the-author TODO never lands."""
        why = self._why()
        assert "TODO" not in why
        assert "Replace this line" not in why
        assert "before requesting review" not in why

    def test_why_states_the_hooks_provenance(self) -> None:
        why = self._why()
        assert AUTO_CREATED_MARKER in why
        assert "No author-written rationale exists" in why

    def test_why_names_the_branch_it_rescued(self) -> None:
        assert "`4424-todo-body`" in self._why(branch="4424-todo-body")

    def test_why_tracks_the_owning_ticket(self) -> None:
        assert f"Tracks {self._ISSUE}." in self._why(issue_url=self._ISSUE)

    def test_the_tracked_issue_carries_no_closing_keyword(self) -> None:
        """A rescue PR is not necessarily the whole fix — merging it must not auto-close the ticket."""
        why = self._why(issue_url=self._ISSUE)
        assert not re.search(r"\b(closes|fixes|resolves)\b", why, re.IGNORECASE)

    def test_unknown_branch_and_ticket_leave_no_dangling_clause(self) -> None:
        why = self._why(branch="  ", issue_url="  ")
        assert "``" not in why
        assert "Tracks" not in why
        assert "the branch is not pushed without a tracking PR" in why


class TestLacksRationale:
    """Which bodies an adopting ship step may overwrite (#3991).

    The predicate fires on the two shapes the no-orphan hook itself produces and on
    nothing else — a body it wrongly claims is placeholder gets an author's rationale
    silently replaced, which is worse than the placeholder it was meant to clear.
    """

    _TITLE = "fix(pr): render a gate-conforming auto-created body"

    def test_the_hook_body_lacks_rationale(self) -> None:
        assert lacks_rationale(auto_created_description(self._TITLE, "- the commit body")) is True

    def test_the_fully_derived_hook_body_still_lacks_rationale(self) -> None:
        """#4424 preservation: a provenance body carrying a branch and a ticket is still not a rationale."""
        body = auto_created_description(
            self._TITLE,
            "- the commit body",
            branch="4424-todo-body",
            issue_url="https://github.com/souliane/teatree/issues/4424",
        )
        assert lacks_rationale(body) is True

    def test_the_placeholder_is_still_found_under_an_appended_section(self) -> None:
        """The hook's body goes through ``ensure_standard_body``, which appends after ``## Why``."""
        body = ensure_standard_body(
            auto_created_description(self._TITLE, "- the commit body"),
            required_sections=["Configuration"],
            section_defaults={"Configuration": "No configuration."},
        )
        assert lacks_rationale(body) is True

    def test_empty_what_why_headings_lack_rationale(self) -> None:
        """The second observed shape: both headings present, nothing under either."""
        assert lacks_rationale(f"{self._TITLE}\n\n## What\n\n## Why\n") is True

    def test_a_blank_body_lacks_rationale(self) -> None:
        assert lacks_rationale("   \n\n") is True

    def test_an_authored_why_is_not_a_placeholder(self) -> None:
        assert lacks_rationale(_GOOD_DESC) is False

    def test_a_body_with_no_why_header_is_left_alone(self) -> None:
        """A human-opened PR using its own headers is never claimed as the hook's."""
        assert lacks_rationale(f"{self._TITLE}\n\n## Summary\nHand-written, different shape.") is False
