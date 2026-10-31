import pytest

from teatree.core.intake.acceptance_criteria import (
    ACCEPTANCE_HEADING,
    CriterionKind,
    acceptance_rows,
    classify_row,
    render_criterion,
)

ISSUE = """\
## Summary

Fix the thing.

## Acceptance criteria

- `t3 publish` rejects a body with a closing keyword
* [ ] ensure-pr opens the PR with one rubric row per bullet
1. the module-health ratchet and CI are green
    and the docs are updated

## Notes

- not an acceptance row
"""


def test_rows_are_the_bullets_under_the_acceptance_heading_only() -> None:
    assert acceptance_rows(ISSUE) == [
        "`t3 publish` rejects a body with a closing keyword",
        "ensure-pr opens the PR with one rubric row per bullet",
        "the module-health ratchet and CI are green and the docs are updated",
    ]


@pytest.mark.parametrize(
    "body",
    ["", "## Summary\n\n- a bullet\n", "## Acceptance\n\nprose only, no bullets\n"],
)
def test_no_rows_when_the_section_is_missing_or_bulletless(body: str) -> None:
    assert acceptance_rows(body) == []


def test_the_acceptance_heading_matches_alternative_spellings() -> None:
    assert ACCEPTANCE_HEADING.search("### Definition of done\n- x")
    assert ACCEPTANCE_HEADING.search("# **Expected behaviour**\n- x")
    assert not ACCEPTANCE_HEADING.search("## Summary\n- x")


@pytest.mark.parametrize(
    ("row", "kind"),
    [
        ("`t3 publish` rejects a body with a closing keyword", CriterionKind.BEHAVIOUR),
        ("the command exits non-zero and writes nothing", CriterionKind.BEHAVIOUR),
        ("uv run tach check and the module-health ratchet are green", CriterionKind.PROCESS),
        ("docs and BLUEPRINT are updated in the same change", CriterionKind.PROCESS),
        ("CI is green", CriterionKind.PROCESS),
        ("review hand-merge records one verdict and refuses a stale order", CriterionKind.BEHAVIOUR),
        ("the commit message names the ticket the command was run for", CriterionKind.BEHAVIOUR),
        ("the change is committed and reviewed", CriterionKind.PROCESS),
    ],
)
def test_a_row_is_typed_behaviour_or_process(row: str, kind: CriterionKind) -> None:
    assert classify_row(row) is kind


def test_render_criterion_prefixes_the_kind_into_the_text_column() -> None:
    assert render_criterion("it refuses", CriterionKind.BEHAVIOUR) == "[behaviour] it refuses"
    assert render_criterion("CI is green", "process") == "[process] CI is green"


def test_render_criterion_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        render_criterion("it refuses", "vibes")
