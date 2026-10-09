"""An approval is spendable only for exactly what was approved: every input the write lands, and the overlay it runs as.

Each test approves one variant through its dry run, then asks for another variant for real: the write must
refuse without sending anything and leave the approval unspent. Dropping an input from the digest leaves a
variant indistinguishable from the approved one, so each test goes red under exactly that mutation.
"""

from typing import Any, cast

import pytest
from django.test import TestCase

from teatree.backends.notion.client import NotionClient
from teatree.backends.notion.errors import NotionBlockChangedError, NotionWriteNotApprovedError
from teatree.cli.notion_write_service import NotionWrites
from teatree.core.models import OnBehalfApproval
from teatree.mcp.services_notion import NewNotionPage, NotionComment
from tests.teatree_backends.notion._fake_notion import install_fake_notion

_OWNER = "alice.example"
_BODY = "## A\n\nbody one\n"
_ROOT_ID = "33333333-3333-3333-3333-333333333333"


class BindingCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        self.notion = install_fake_notion(monkeypatch)
        self.notion.page_parent = {"type": "page_id", "page_id": _ROOT_ID}
        monkeypatch.setattr("teatree.backends.notion.write_guard.notion_write_roots", lambda _overlay: ([_ROOT_ID], []))

    def writes(self, overlay: str | None = None) -> NotionWrites:
        return NotionWrites(NotionClient(token="test-token", overlay=overlay))

    def approve(self, dry_run: dict[str, Any], action: str) -> None:
        OnBehalfApproval.record(dry_run["approval"]["target"], action, _OWNER)

    def assert_refused_and_unspent(self, real_write: Any) -> None:
        with pytest.raises(NotionBlockChangedError, match="different text, or another overlay"):
            real_write()
        assert [request for request in self.notion.requests if request[0] != "GET"] == []
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1


class TestEveryInputOfAReplaceIsBound(BindingCase):
    def replace(
        self, old: str = "every quarter", new: str = "monthly", *, dry_run: bool, overlay: str | None = None
    ) -> Any:
        return self.writes(overlay).replace(self.notion.page_id, old, new, dry_run=dry_run)

    def test_the_new_text(self) -> None:
        block = self.notion.paragraph("Rates reset every quarter.")
        self.approve(self.replace(dry_run=True), "notion_replace")

        self.assert_refused_and_unspent(lambda: self.replace(new="never", dry_run=False))

        assert self.notion.text_of(block) == "Rates reset every quarter."

    def test_the_old_text(self) -> None:
        block = self.notion.paragraph("Rates reset every quarter.")
        self.approve(self.replace(dry_run=True), "notion_replace")

        self.assert_refused_and_unspent(lambda: self.replace(old="Rates", dry_run=False))

        assert self.notion.text_of(block) == "Rates reset every quarter."


class TestEveryInputOfACreateIsBound(BindingCase):
    def page(self, **over: str) -> NewNotionPage:
        return NewNotionPage(**{"parent": self.notion.page_id, "title": "CTO brief", "body": _BODY, "icon": "🏗️"} | over)

    def test_the_body(self) -> None:
        self.approve(self.writes().create(self.page(), dry_run=True), "notion_create")

        self.assert_refused_and_unspent(
            lambda: self.writes().create(self.page(body="## A\n\nbody TWO\n"), dry_run=False)
        )

        assert ("POST", "/pages") not in self.notion.requests

    def test_the_icon(self) -> None:
        self.approve(self.writes().create(self.page(), dry_run=True), "notion_create")

        self.assert_refused_and_unspent(lambda: self.writes().create(self.page(icon="🔥"), dry_run=False))

        assert ("POST", "/pages") not in self.notion.requests


class TestEveryInputOfACommentIsBound(BindingCase):
    def comment(self, **over: str) -> NotionComment:
        return NotionComment(**{"page": self.notion.page_id, "body": "first body", "marker": "[m1]"} | over)

    def test_the_body(self) -> None:
        self.approve(self.writes().comment(self.comment(), dry_run=True), "notion_comment")

        self.assert_refused_and_unspent(lambda: self.writes().comment(self.comment(body="second body"), dry_run=False))

        assert self.notion.comments == []

    def test_the_marker(self) -> None:
        self.approve(self.writes().comment(self.comment(), dry_run=True), "notion_comment")

        self.assert_refused_and_unspent(lambda: self.writes().comment(self.comment(marker="[m2]"), dry_run=False))

        assert self.notion.comments == []

    def test_the_quote_on_the_same_block(self) -> None:
        self.notion.paragraph("Rates reset every quarter. A fixed rate applies.")
        self.approve(self.writes().comment(self.comment(quote="every quarter"), dry_run=True), "notion_comment")

        self.assert_refused_and_unspent(
            lambda: self.writes().comment(self.comment(quote="A fixed rate"), dry_run=False)
        )

        assert self.notion.comments == []


class TestTheDirectionOfAnArchiveIsBound(BindingCase):
    def test_the_two_directions_have_distinct_targets(self) -> None:
        archive = cast(
            "dict[str, str]", self.writes().archive(self.notion.page_id, archived=True, dry_run=True)["approval"]
        )["target"]
        self.notion.page_archived = True

        restore = cast(
            "dict[str, str]", self.writes().archive(self.notion.page_id, archived=False, dry_run=True)["approval"]
        )["target"]

        assert archive != restore

    def test_an_archive_approval_is_not_spendable_on_a_restore(self) -> None:
        self.approve(self.writes().archive(self.notion.page_id, archived=True, dry_run=True), "notion_archive")
        self.notion.page_archived = True

        with pytest.raises(NotionWriteNotApprovedError, match="no recorded approval covers this write"):
            self.writes().archive(self.notion.page_id, archived=False, dry_run=False)

        assert self.notion.page_archived is True
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1

    def test_a_restore_approval_is_not_spendable_on_an_archive(self) -> None:
        self.notion.page_archived = True
        self.approve(self.writes().archive(self.notion.page_id, archived=False, dry_run=True), "notion_restore")
        self.notion.page_archived = False

        with pytest.raises(NotionWriteNotApprovedError, match="no recorded approval covers this write"):
            self.writes().archive(self.notion.page_id, archived=True, dry_run=False)

        assert self.notion.page_archived is False
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1


class TestEveryInputOfAPropertySetIsBound(BindingCase):
    def write_property(self, *, name: str = "Status", value: str = "Ready", dry_run: bool) -> Any:
        return self.writes().property_set(self.notion.page_id, name, value, dry_run=dry_run)

    def setUp(self) -> None:
        super().setUp()
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Draft"}})
        self.notion.set_property("Owner", {"type": "rich_text", "rich_text": []})

    def test_the_value(self) -> None:
        self.approve(self.write_property(dry_run=True), "notion_property_set")

        self.assert_refused_and_unspent(lambda: self.write_property(value="Done", dry_run=False))

        assert self.notion.properties["Status"]["status"] == {"name": "Draft"}

    def test_the_property_name(self) -> None:
        self.approve(self.write_property(name="Owner", value="Ready", dry_run=True), "notion_property_set")

        self.assert_refused_and_unspent(lambda: self.write_property(name="Status", value="Ready", dry_run=False))

        assert self.notion.properties["Status"]["status"] == {"name": "Draft"}


class TestTheOverlayIsBound(BindingCase):
    def test_an_approval_recorded_after_a_dry_run_under_one_overlay_is_not_spendable_under_another(self) -> None:
        block = self.notion.paragraph("Rates reset every quarter.")
        args = (self.notion.page_id, "every quarter", "monthly")
        self.approve(self.writes("acme").replace(*args, dry_run=True), "notion_replace")

        self.assert_refused_and_unspent(lambda: self.writes("other").replace(*args, dry_run=False))

        assert self.notion.text_of(block) == "Rates reset every quarter."

    def test_the_same_overlay_spends_it(self) -> None:
        block = self.notion.paragraph("Rates reset every quarter.")
        args = (self.notion.page_id, "every quarter", "monthly")
        self.approve(self.writes("acme").replace(*args, dry_run=True), "notion_replace")

        written: dict[str, Any] = self.writes("acme").replace(*args, dry_run=False)

        assert written["writing_as"]["overlay"] == "acme"
        assert self.notion.text_of(block) == "Rates reset monthly."

    def test_a_create_approved_under_one_overlay_is_not_spendable_under_another(self) -> None:
        page = NewNotionPage(self.notion.page_id, "CTO brief", _BODY)
        self.approve(self.writes("acme").create(page, dry_run=True), "notion_create")

        self.assert_refused_and_unspent(lambda: self.writes("other").create(page, dry_run=False))

        assert ("POST", "/pages") not in self.notion.requests

    def test_a_comment_approved_under_one_overlay_is_not_spendable_under_another(self) -> None:
        comment = NotionComment(self.notion.page_id, "first body")
        self.approve(self.writes("acme").comment(comment, dry_run=True), "notion_comment")

        self.assert_refused_and_unspent(lambda: self.writes("other").comment(comment, dry_run=False))

        assert self.notion.comments == []

    def test_every_dry_run_names_the_integration_and_overlay_it_would_write_as(self) -> None:
        self.notion.paragraph("Rates reset every quarter.")
        dry_runs = [
            self.writes("acme").replace(self.notion.page_id, "every quarter", "monthly", dry_run=True),
            self.writes("acme").create(NewNotionPage(self.notion.page_id, "CTO brief", _BODY), dry_run=True),
            self.writes("acme").comment(NotionComment(self.notion.page_id, "first body"), dry_run=True),
        ]

        for payload in dry_runs:
            assert payload["writing_as"] == {"integration": "Factory", "bot_id": "bot-1", "overlay": "acme"}

    def test_an_archive_approved_under_one_overlay_is_not_spendable_under_another(self) -> None:
        self.approve(self.writes("acme").archive(self.notion.page_id, archived=True, dry_run=True), "notion_archive")

        self.assert_refused_and_unspent(
            lambda: self.writes("other").archive(self.notion.page_id, archived=True, dry_run=False)
        )

        assert self.notion.page_archived is False

    def test_a_property_set_approved_under_one_overlay_is_not_spendable_under_another(self) -> None:
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Draft"}})
        args = (self.notion.page_id, "Status", "Ready")
        self.approve(self.writes("acme").property_set(*args, dry_run=True), "notion_property_set")

        self.assert_refused_and_unspent(lambda: self.writes("other").property_set(*args, dry_run=False))

        assert self.notion.properties["Status"]["status"] == {"name": "Draft"}

    def test_the_archive_and_property_dry_runs_name_the_integration_and_overlay_they_would_write_as(self) -> None:
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Draft"}})
        dry_runs = [
            self.writes("acme").archive(self.notion.page_id, archived=True, dry_run=True),
            self.writes("acme").property_set(self.notion.page_id, "Status", "Ready", dry_run=True),
        ]

        for payload in dry_runs:
            assert payload["writing_as"] == {"integration": "Factory", "bot_id": "bot-1", "overlay": "acme"}
