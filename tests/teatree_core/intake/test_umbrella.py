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


class TestTitleIsNotASignal:
    """(2) alone is refused: a prefix convention breaks the first time an epic is titled differently."""

    def test_an_epic_prefixed_body_with_no_other_signal_is_admitted(self) -> None:
        assert _reason("Epic: forge portability\n\nOne bounded fix, actually.") == ""
