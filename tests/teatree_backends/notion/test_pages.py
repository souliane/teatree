"""The page-create dry run: what a create would do, decided before anything is written."""

import pytest

from teatree.backends.notion.blocks import build_blocks
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionWriteRefusedError
from teatree.backends.notion.pages import PageCreator
from tests.teatree_backends.notion._fake_notion import FakeNotion

_BODY = "## Scope\n\nThe factory owns the build, never the request path.\n"


def _create(notion: FakeNotion, *, dry_run: bool) -> tuple[str, int]:
    creator = PageCreator(NotionClient(token="good"))
    blocks = build_blocks(_BODY)
    if dry_run:
        found = creator.dry_run(notion.page_id, title="CTO brief", blocks=blocks, markdown=_BODY)
    else:
        found = creator.create(notion.page_id, title="CTO brief", blocks=blocks, icon="", markdown=_BODY)
    return found.outcome, found.blocks


class TestDryRun:
    def test_a_dry_run_names_the_create_and_sends_no_write(self, notion: FakeNotion) -> None:
        outcome, blocks = _create(notion, dry_run=True)

        assert (outcome, blocks) == ("would create", 2)
        assert {method for method, _path in notion.requests} == {"GET"}
        assert notion.pages == {}

    def test_a_dry_run_reports_a_page_already_carrying_the_title_as_existing(self, notion: FakeNotion) -> None:
        assert _create(notion, dry_run=False)[0] == "created"

        assert _create(notion, dry_run=True)[0] == "exists"

    def test_a_dry_run_under_a_parent_outside_the_write_roots_is_refused(
        self, notion: FakeNotion, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: ([], []))

        with pytest.raises(NotionWriteRefusedError):
            _create(notion, dry_run=True)
