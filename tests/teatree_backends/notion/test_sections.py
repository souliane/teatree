"""The owned-section write contract, exercised end to end against a fake Notion."""

import pytest

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionAmbiguousSectionError, NotionWriteNotLandedError
from teatree.backends.notion.sections import SectionLocator, SectionWriter, normalize_heading
from tests.teatree_backends.notion._fake_notion import FakeNotion

CANONICAL = "🔧 /prd-agent — engineering delivery notes"
LEGACY = ("🔧 Engineering delivery notes", "🔧 Engineering verification notes")
BODY = "### Delivered in\n\n- MR !1, 12 files\n\n### NOT verified\n\nThe live deployment push."


def _writer(notion: FakeNotion) -> tuple[SectionWriter, SectionLocator, NotionClient]:
    client = NotionClient(token="good")
    locator = SectionLocator(client, canonical=CANONICAL, legacy=LEGACY)
    return SectionWriter(client, locator), locator, client


class TestHeadingNormalization:
    @pytest.mark.parametrize(
        "heading",
        [
            '## 🔧 /prd-agent — engineering delivery notes {toggle="true"}',
            "🔧 /prd-agent — engineering delivery notes",
            "/prd-agent - Engineering Delivery Notes",
            "🔧  /prd-agent \u2013 engineering  delivery notes",
        ],
    )
    def test_every_surface_form_reduces_to_the_same_key(self, heading: str) -> None:
        assert normalize_heading(heading) == "/prd-agent - engineering delivery notes"

    def test_a_different_skills_section_is_not_claimed(self) -> None:
        assert normalize_heading("🧪 /bdd-test-creation — scenarios and verification status") != normalize_heading(
            CANONICAL
        )


class TestSectionResolution:
    def test_a_toggle_headings_body_is_its_children(self, notion: FakeNotion) -> None:
        notion.paragraph("PRD body nobody may touch")
        heading = notion.heading(CANONICAL, toggle=True)
        notion.blocks[heading]["has_children"] = True
        owned = notion.paragraph("old delivery notes", parent=heading)

        _, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)

        assert section is not None
        assert section.toggle is True
        assert section.body_block_ids == (owned,)

    def test_a_plain_headings_body_stops_at_the_next_h2(self, notion: FakeNotion) -> None:
        heading = notion.heading(CANONICAL)
        first = notion.paragraph("old notes")
        second = notion.paragraph("more old notes")
        notion.heading("🧪 /bdd-test-creation — scenarios and verification status")
        notion.paragraph("the other skill's body")

        _, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)

        assert section is not None
        assert section.heading_id == heading
        assert section.body_block_ids == (first, second)

    def test_a_legacy_heading_is_adopted_not_duplicated(self, notion: FakeNotion) -> None:
        notion.heading(LEGACY[0], toggle=True)

        _, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)

        assert section is not None
        assert section.matched_legacy is True

    def test_two_matching_headings_stop_the_write(self, notion: FakeNotion) -> None:
        notion.heading(CANONICAL, toggle=True)
        notion.heading(LEGACY[1], toggle=True)

        _, locator, _ = _writer(notion)

        with pytest.raises(NotionAmbiguousSectionError, match="A duplicate means an earlier run went wrong"):
            locator.resolve(notion.page_id)

    def test_an_absent_section_resolves_to_none(self, notion: FakeNotion) -> None:
        notion.paragraph("just the PRD body")

        _, locator, _ = _writer(notion)

        assert locator.resolve(notion.page_id) is None


class TestSectionReplace:
    def test_replacing_leaves_every_other_block_untouched(self, notion: FakeNotion) -> None:
        prd_body = notion.paragraph("High level description")
        heading = notion.heading(CANONICAL, toggle=True)
        notion.blocks[heading]["has_children"] = True
        stale = notion.paragraph("stale delivery notes", parent=heading)
        other_skill = notion.heading("🧪 /bdd-test-creation — scenarios and verification status")

        writer, _, _ = _writer(notion)
        section = writer._locator.resolve(notion.page_id)
        assert section is not None
        result = writer.replace(notion.page_id, section, BODY)

        assert result.outcome == "replaced"
        assert notion.archived == [stale], "only the section's own body blocks are archived"
        assert prd_body in notion.children[notion.page_id]
        assert other_skill in notion.children[notion.page_id]
        assert "Delivered in" in " ".join(notion.body_texts(heading))
        assert "stale delivery notes" not in " ".join(notion.body_texts(heading))

    def test_the_heading_block_survives_so_its_discussions_do(self, notion: FakeNotion) -> None:
        heading = notion.heading(CANONICAL, toggle=True)
        notion.paragraph("stale", parent=heading)

        writer, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)
        assert section is not None
        writer.replace(notion.page_id, section, BODY)

        assert heading not in notion.archived
        resolved = locator.resolve(notion.page_id)
        assert resolved is not None
        assert resolved.heading_id == heading

    def test_adopting_a_legacy_heading_renames_it_in_place(self, notion: FakeNotion) -> None:
        heading = notion.heading(LEGACY[0], toggle=True)
        notion.paragraph("stale", parent=heading)

        writer, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)
        assert section is not None
        writer.replace(notion.page_id, section, BODY)

        assert heading not in notion.archived, "renaming must not re-create the block"
        assert notion.text_of(heading) == CANONICAL

    def test_a_plain_section_body_lands_between_its_own_headings(self, notion: FakeNotion) -> None:
        heading = notion.heading(CANONICAL)
        stale = notion.paragraph("stale notes")
        next_heading = notion.heading("🧪 /bdd-test-creation — scenarios and verification status")

        writer, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)
        assert section is not None
        writer.replace(notion.page_id, section, BODY)

        order = notion.children[notion.page_id]
        assert stale not in order
        assert order.index(heading) < order.index(next_heading)
        between = order[order.index(heading) + 1 : order.index(next_heading)]
        assert "Delivered in" in " ".join(notion.text_of(block_id) for block_id in between)

    def test_a_write_that_does_not_land_is_reported_as_failed(self, notion: FakeNotion) -> None:
        heading = notion.heading(CANONICAL, toggle=True)
        notion.paragraph("stale", parent=heading)

        writer, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)
        assert section is not None
        notion.suppress_appends = True

        with pytest.raises(NotionWriteNotLandedError, match="does not contain"):
            writer.replace(notion.page_id, section, BODY)

    def test_surviving_old_blocks_are_reported_as_failed(self, notion: FakeNotion) -> None:
        heading = notion.heading(CANONICAL, toggle=True)
        notion.paragraph("stale", parent=heading)

        writer, locator, _ = _writer(notion)
        section = locator.resolve(notion.page_id)
        assert section is not None
        notion.suppress_deletes = True

        with pytest.raises(NotionWriteNotLandedError, match="old block"):
            writer.replace(notion.page_id, section, BODY)


class TestSectionCreate:
    def test_creating_appends_a_toggle_heading_with_the_canonical_string(self, notion: FakeNotion) -> None:
        notion.paragraph("High level description")

        writer, locator, _ = _writer(notion)
        result = writer.create(notion.page_id, BODY)

        assert result.outcome == "created"
        section = locator.resolve(notion.page_id)
        assert section is not None
        assert section.heading_text == CANONICAL
        assert section.toggle is True
        assert "Delivered in" in " ".join(notion.body_texts(section.heading_id))

    def test_creating_twice_would_be_caught_as_a_duplicate(self, notion: FakeNotion) -> None:
        writer, _, _ = _writer(notion)
        writer.create(notion.page_id, BODY)

        with pytest.raises(NotionAmbiguousSectionError):
            writer.create(notion.page_id, BODY)
