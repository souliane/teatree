"""Shared Notion identity parsing for backend and cold hooks."""

import pytest

from teatree.hooks.notion_ids import canonical_notion_id, is_notion_object_id

PAGE = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
DATABASE = "99999999-8888-4777-8666-555555555555"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (PAGE, PAGE),
        (PAGE.replace("-", ""), PAGE),
        (f"https://www.notion.so/Title-{PAGE.replace('-', '')}", PAGE),
        (f"https://www.notion.so/{DATABASE}?v={PAGE}", DATABASE),
        (f"https://www.notion.so/{DATABASE}?v={DATABASE}&p={PAGE}&pm=s", PAGE),
        (f"https://www.notion.so/?p={PAGE}", PAGE),
        ("https://www.notion.so/no-id", None),
    ],
)
def test_canonical_notion_id(reference: str, expected: str | None) -> None:
    assert canonical_notion_id(reference) == expected


@pytest.mark.parametrize(
    ("reference", "expected"),
    [(PAGE, True), (PAGE.replace("-", ""), True), (f"Title-{PAGE}", False), ("no-id", False)],
)
def test_is_notion_object_id(reference: str, *, expected: bool) -> None:
    assert is_notion_object_id(reference) is expected
