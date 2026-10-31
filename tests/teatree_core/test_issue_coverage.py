import pytest

from teatree.core.issue_coverage import acceptance_items, plain, uncovered, unechoed

_ISSUE = """Intro paragraph.

- a bullet outside any acceptance section

## Acceptance

1. Merging a pull request leaves the ticket out of `merged`.
2) A regression test goes through the real merge path.
- [x] already done by the author
- [ ] a read-only report lists the tickets
* **Bold item**: with a clause.

## Notes

- [ ] unrelated box

## Request added (2026-10-31)

### Acceptance

- A later dated item.
- No new setting key.

## Definition of Done
- The docs are updated
"""


def test_every_open_item_under_every_acceptance_heading_is_an_item() -> None:
    assert acceptance_items(_ISSUE) == (
        "Merging a pull request leaves the ticket out of `merged`.",
        "A regression test goes through the real merge path.",
        "a read-only report lists the tickets",
        "**Bold item**: with a clause.",
        "A later dated item.",
        "No new setting key.",
        "The docs are updated",
    )


@pytest.mark.parametrize("body", ["", "No headings at all.", "## Acceptance\n\nProse only, no list.\n"])
def test_an_issue_without_listed_items_has_none(body: str) -> None:
    assert acceptance_items(body) == ()


def test_the_same_item_listed_twice_is_one_item() -> None:
    assert acceptance_items("## Acceptance\n- Same.\n\n## Acceptance\n- Same.\n") == ("Same.",)


def test_plain_ignores_case_markdown_marks_and_spacing() -> None:
    assert plain("A  **Bold**\n`code` x ") == "a bold code x"


def test_a_delivered_text_quoting_every_item_leaves_none_unechoed() -> None:
    items = ("Merging a pull request leaves the ticket out of merged.", "No new setting key.")
    delivered = "Merging a pull request leaves the ticket **out of merged**\n\n- no new setting key"

    assert unechoed(items, delivered) == ()


def test_an_item_the_delivered_text_omits_is_unechoed() -> None:
    items = ("First thing.", "Second thing.")

    assert unechoed(items, "Delivered: first thing") == ("Second thing.",)


def test_an_item_is_covered_by_a_criterion_that_restates_it() -> None:
    items = ("A regression test goes through the real merge path.", "No new setting key.")
    criteria = ("A regression test goes through the real merge path, and it fails on main.",)

    assert uncovered(items, criteria) == ("No new setting key.",)


def test_a_criterion_shorter_than_the_item_does_not_cover_it() -> None:
    assert uncovered(("Tests pass and the docs are updated.",), ("tests pass",)) == (
        "Tests pass and the docs are updated.",
    )
