"""Archive and restore a page: one boolean on the wire, and a success the re-read has to confirm."""

import pytest

from teatree.backends.notion.archive import PageArchiver
from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionWriteNotLandedError
from tests.teatree_backends.notion._fake_notion import FakeNotion


@pytest.fixture
def archiver(monkeypatch: pytest.MonkeyPatch, notion: FakeNotion) -> PageArchiver:
    root = "33333333-3333-3333-3333-333333333333"
    notion.page_parent = {"type": "page_id", "page_id": root}
    monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: ([root], []))
    return PageArchiver(NotionClient(token="good"))


class TestWrite:
    def test_archiving_sends_the_one_boolean_and_the_page_reads_back_as_archived(
        self, archiver: PageArchiver, notion: FakeNotion
    ) -> None:
        archiver.write(notion.page_id, archived=True)

        assert notion.page_patches == [{"archived": True}]
        assert notion.page_archived is True

    def test_restoring_sends_the_same_boolean_the_other_way(self, archiver: PageArchiver, notion: FakeNotion) -> None:
        notion.page_archived = True

        archiver.write(notion.page_id, archived=False)

        assert notion.page_patches == [{"archived": False}]
        assert notion.page_archived is False

    @pytest.mark.parametrize(
        ("archived", "reads_as", "wanted"), [(True, "live", "archived"), (False, "archived", "live")]
    )
    def test_a_patch_notion_answered_200_without_applying_is_not_reported_as_landed(
        self, archiver: PageArchiver, notion: FakeNotion, *, archived: bool, reads_as: str, wanted: str
    ) -> None:
        notion.page_archived = not archived
        notion.suppress_archive = True

        with pytest.raises(NotionWriteNotLandedError, match=f"still reads as {reads_as}, not {wanted}"):
            archiver.write(notion.page_id, archived=archived)
