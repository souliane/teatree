"""The umbrella/epic detector — a tracking parent is never an implementable unit (#4105)."""

from teatree.config.schema import shipped_defaults
from teatree.core.intake.umbrella import umbrella_reason

#: The shipped ``umbrella_issue_labels`` value, read where it lives rather than restated.
SHIPPED_LABELS = frozenset(shipped_defaults().umbrella_issue_labels)

#: The real shape of souliane/teatree#4048, the epic the factory claimed.
EPIC_4048 = """## Why these are one thing

Required checks go red for reasons owned by the harness.

## Members

- [x] #3848
- [x] #3892
- [x] #4035

Each member keeps its own thread until it is folded here.
"""

BUG_BODY = """## Observed

The scanner drops the last candidate.

## Acceptance

- The scanner keeps every candidate.
- A regression test pins the count.
"""

#: The real shape of souliane/teatree#2663, the standing ledger the factory claimed. It carries
#: no label, and every checklist item carries prose, so neither of the first two signals fires.
LEDGER_2663 = """## ⚠️ DO NOT CLOSE — standing ledger

This is a **permanent, reusable tracking issue**. It is **never closed**.

### How to use it (each pass)

1. Run the pass.
2. Append a new section below.

- [ ] **Dream engine misses raw transcript drift** — keyword-gated extraction.
- [ ] **Review-comment-bloat gate never built** — comment bloat keeps recurring.
"""


def _reason(body: str, labels: frozenset[str] = frozenset()) -> str:
    return umbrella_reason(body=body, labels=labels, umbrella_labels=SHIPPED_LABELS)


class TestLabelSignal:
    """The explicit marker — option (3), an operator-maintained exclusion list."""

    def test_epic_label_is_umbrella(self) -> None:
        assert "epic" in _reason("anything at all", frozenset({"epic"}))

    def test_label_match_is_case_and_space_insensitive(self) -> None:
        assert _reason("anything", frozenset({" Epic "}))

    def test_an_unlisted_label_is_not_a_signal(self) -> None:
        assert _reason(BUG_BODY, frozenset({"bug", "p1"})) == ""

    def test_the_label_set_is_data_not_a_hardcoded_constant(self) -> None:
        assert umbrella_reason(body="", labels=frozenset({"parent"}), umbrella_labels=frozenset({"parent"}))
        assert umbrella_reason(body="", labels=frozenset({"epic"}), umbrella_labels=frozenset({"parent"})) == ""


class TestStructuralSignal:
    """Option (1) — detect the SHAPE, so an unlabelled epic is caught too."""

    def test_a_child_checklist_with_no_acceptance_criteria_is_umbrella(self) -> None:
        reason = _reason(EPIC_4048)
        assert "3" in reason
        assert "child" in reason

    def test_a_gitlab_child_url_checklist_counts(self) -> None:
        body = "## Members\n\n- [ ] https://gitlab.com/acme/app/-/issues/12\n- [ ] https://gitlab.com/acme/app/-/issues/13\n"
        assert _reason(body)

    def test_a_non_forge_url_checklist_is_not_a_child_list(self) -> None:
        """A docs-link checklist is a plan, not a tracking parent — the URL grammar is narrow."""
        body = "## Docs\n\n- [ ] https://example.com/readme\n- [ ] https://example.com/changelog\n"
        assert _reason(body) == ""

    def test_a_forge_url_with_a_trailing_fragment_is_not_a_bare_child_ref(self) -> None:
        """The link must span the WHOLE token — a comment-anchored URL is prose about a child."""
        body = (
            "## Members\n\n"
            "- [ ] https://github.com/o/r/issues/12#issuecomment-1\n"
            "- [ ] https://github.com/o/r/issues/13#issuecomment-2\n"
        )
        assert _reason(body) == ""

    def test_one_child_link_is_below_the_threshold(self) -> None:
        assert _reason("## Members\n\n- [x] #3814\n") == ""

    def test_repeating_one_child_ref_does_not_reach_the_threshold(self) -> None:
        assert _reason("- [ ] #3814\n- [x] #3814\n") == ""

    def test_acceptance_criteria_defeat_the_structural_signal(self) -> None:
        assert _reason(EPIC_4048 + "\n## Acceptance\n\n- intake declines it\n") == ""

    def test_a_definition_of_done_heading_defeats_it_too(self) -> None:
        assert _reason(EPIC_4048 + "\n### Definition of done\n\n- it lands\n") == ""

    def test_both_spellings_of_the_expected_behaviour_heading_defeat_it(self) -> None:
        """Pinned per spelling: the codespell hook rewrites an optional-letter alternation."""
        for heading in ("## Expected behaviour", "## Expected behavior"):
            assert _reason(f"{EPIC_4048}\n{heading}\n\n- it declines the epic\n") == "", heading

    def test_a_prose_reference_to_other_issues_is_not_a_child_checklist(self) -> None:
        assert _reason("Related: #4065 and #4041 both touch this.\nSee also #4048.\n") == ""

    def test_an_implementable_checklist_of_prose_criteria_is_not_umbrella(self) -> None:
        assert _reason("## Plan\n\n- [ ] add the detector\n- [ ] wire the scanner\n- [ ] test it\n") == ""

    def test_a_checklist_item_with_trailing_prose_is_not_a_bare_child_ref(self) -> None:
        assert _reason("- [ ] #10 rewrite the parser\n- [ ] #11 delete the shim\n") == ""

    def test_an_empty_body_is_not_umbrella(self) -> None:
        assert _reason("") == ""


class TestStandingLedgerSignal:
    """The row DECLARES itself never closed — the third signal (souliane/teatree#2663)."""

    def test_the_standing_ledger_is_umbrella(self) -> None:
        assert "never closed" in _reason(LEDGER_2663)

    def test_a_bold_declaration_needs_no_heading(self) -> None:
        assert _reason("**DO NOT CLOSE**\n\nA rolling log.\n")

    def test_the_wording_variants_all_declare_it(self) -> None:
        for line in ("## Do not close", "## Don't close this issue", "**Never close this ticket.**"):
            assert _reason(f"{line}\n\nA rolling log.\n"), line

    def test_acceptance_criteria_do_not_defeat_an_explicit_declaration(self) -> None:
        """Unlike the inferred structural shape: a stated criterion does not make the row closable."""
        assert _reason(f"{LEDGER_2663}\n## Acceptance\n\n- an entry is appended\n")

    def test_unemphasised_prose_asking_to_hold_the_issue_is_not_a_signal(self) -> None:
        """A temporary hold on implementable work — declining it would starve a real ticket."""
        assert _reason("Do not close this until the follow-up lands.\n") == ""

    def test_a_heading_naming_another_object_is_not_a_signal(self) -> None:
        assert _reason("## Do not close the connection pool\n\nIt is pooled per request.\n") == ""

    def test_a_body_merely_mentioning_closing_is_not_a_signal(self) -> None:
        assert _reason("## Observed\n\nThe reader does not close the file handle.\n") == ""

    def test_the_declaration_must_lead_the_line(self) -> None:
        assert _reason("## Rule: do not close this issue\n") == ""

    def test_an_unemphasised_bare_directive_ending_in_a_period_is_not_a_signal(self) -> None:
        """Kills the mutant that drops the emphasis requirement: plain prose, not a heading/bold line."""
        assert _reason("Do not close.\n") == ""

    def test_a_directive_qualified_by_a_colon_describes_an_object_not_the_row(self) -> None:
        assert _reason("## Do not close: the modal stays open after submit\n") == ""

    def test_a_directive_joined_by_a_hyphen_is_a_compound_word_not_a_declaration(self) -> None:
        assert _reason("## Do not close-fail the socket\n") == ""

    def test_a_directive_followed_by_a_comma_clause_is_not_a_declaration(self) -> None:
        assert _reason("**Never close, then reopen, the writer**\n") == ""

    def test_a_dash_introducing_an_aside_still_declares_it(self) -> None:
        assert _reason("## Do not close — see the umbrella docs\n")

    def test_a_declaration_quoted_inside_a_fenced_code_block_is_not_live(self) -> None:
        """Documenting the detector is not invoking it — even alongside an Acceptance heading."""
        body = (
            "## Observed\n\nThe docs example renders wrong:\n\n"
            "```markdown\n## DO NOT CLOSE — standing ledger\n```\n\n"
            "## Acceptance\n- the fence renders\n"
        )
        assert _reason(body) == ""

    def test_the_same_declaration_unfenced_still_fires(self) -> None:
        assert _reason("## DO NOT CLOSE — standing ledger\n")


class TestTitleIsNotASignal:
    """(2) alone is refused: a prefix convention breaks the first time an epic is titled differently."""

    def test_an_epic_prefixed_body_with_no_other_signal_is_admitted(self) -> None:
        assert _reason("Epic: forge portability\n\nOne bounded fix, actually.") == ""
