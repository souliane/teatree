"""Archive and property MCP writes through the recorded approval and HTTP fake."""

from typing import Any

from asgiref.sync import async_to_sync

from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture
from tests.teatree_mcp.test_services_notion_writes import _ROOTS, NotionWriteCase, _run

_ARCHIVE = "notion_archive"
_RESTORE = "notion_restore"
_PROPERTY = "notion_property_set"
_ROOT_ID = "33333333-3333-3333-3333-333333333333"
_DATABASE_ID = "44444444-4444-4444-4444-444444444444"
_INNER_ROOT_ID = "55555555-5555-5555-5555-555555555555"


class ArchiveCase(NotionWriteCase):
    def setUp(self) -> None:
        super().setUp()
        self.notion.page_parent = {"type": "page_id", "page_id": _ROOT_ID}
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([_ROOT_ID, _DATABASE_ID], []))

    def archive(self, *, dry_run: bool, archived: bool = True) -> dict[str, Any]:
        return self.call("notion_archive", page=self.notion.page_id, archived=archived, dry_run=dry_run)

    def refused_archive(self, *, dry_run: bool, archived: bool = True) -> str:
        return self.refused("notion_archive", page=self.notion.page_id, archived=archived, dry_run=dry_run)

    def approved(self, *, archived: bool = True) -> OnBehalfApproval:
        return self.approve(self.archive(dry_run=True, archived=archived), _ARCHIVE if archived else _RESTORE)

    def make_liveness_unprovable(self) -> None:
        self.notion.make_database_row(database_id=_DATABASE_ID, title="pricing tooltip")
        self.notion.rows = [{"id": "22222222222222222222222222222222"}]


class TestAnArchiveIsADryRunByDefault(ArchiveCase):
    def test_a_call_without_dry_run_writes_nothing_and_names_the_approval_it_needs(self) -> None:
        self.notion.set_property("Name", {"type": "title", "title": [_run("Pricing notes")]})

        payload = self.call("notion_archive", page=self.notion.page_id)

        assert (payload["outcome"], payload["would"]) == ("dry_run", "archive")
        assert (payload["page_id"], payload["title"]) == (self.notion.page_id, "Pricing notes")
        assert (payload["writing_as"]["integration"], payload["writing_as"]["bot_id"]) == ("Factory", "bot-1")
        assert payload["approval"]["action"] == _ARCHIVE
        assert payload["approval"]["target"].startswith(f"notion:{self.notion.page_id}:")
        assert payload["approval"]["record_command"].endswith(f" {_ARCHIVE} --approver <owner-id>")
        assert self.mutations() == []
        assert self.notion.page_archived is False
        assert not OnBehalfApproval.objects.exists()

    def test_a_restore_dry_run_names_the_direction_and_writes_nothing(self) -> None:
        self.notion.page_archived = True

        payload = self.archive(dry_run=True, archived=False)

        assert (payload["outcome"], payload["would"]) == ("dry_run", "restore")
        assert payload["approval"]["action"] == _RESTORE
        assert self.mutations() == []
        assert self.notion.page_archived is True


class TestAnArchiveNeedsARecordedApproval(ArchiveCase):
    def test_without_one_the_write_is_refused_with_exit_23_and_nothing_is_sent(self) -> None:
        message = self.refused_archive(dry_run=False)

        assert message.startswith("notion exit 23 (NotionWriteNotApprovedError)")
        assert "approve-on-behalf" in message
        assert self.mutations() == []
        assert self.notion.page_archived is False

    def test_dry_run_then_record_then_write_trashes_the_page_verified_spends_the_approval_and_audits_it(self) -> None:
        approval = self.approved()

        payload = self.archive(dry_run=False)

        assert (payload["outcome"], payload["verified"]) == ("archived", True)
        assert (payload["writing_as"]["integration"], payload["writing_as"]["bot_id"]) == ("Factory", "bot-1")
        assert self.mutations() == [("PATCH", f"/pages/{self.notion.page_id}")]
        assert self.notion.page_patches == [{"archived": True}]
        assert self.notion.page_archived is True
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get().approval_id == approval.pk

    def test_the_same_call_again_is_already_applied_and_sends_no_second_write(self) -> None:
        self.approved()
        self.archive(dry_run=False)
        sent = len(self.mutations())

        payload = self.archive(dry_run=False)

        assert payload["outcome"] == "already applied"
        assert "approval" not in payload
        assert len(self.mutations()) == sent

    def test_an_approval_is_single_use(self) -> None:
        self.approved()
        self.archive(dry_run=False)
        self.notion.page_archived = False

        assert self.refused_archive(dry_run=False).startswith("notion exit 23")
        assert self.notion.page_archived is False

    def test_a_permitting_posture_and_an_allowlisted_action_do_not_stand_in_for_the_approval(self) -> None:
        seed_permitting_posture()
        ConfigSetting.objects.set_value("on_behalf_auto_actions", [_ARCHIVE])

        assert self.refused_archive(dry_run=False).startswith("notion exit 23")
        assert self.mutations() == []

    def test_an_archive_approval_is_not_spendable_on_a_restore(self) -> None:
        approval = self.approved()
        self.notion.page_archived = True

        message = self.refused_archive(dry_run=False, archived=False)

        assert message.startswith("notion exit 23 (NotionWriteNotApprovedError)")
        assert self.mutations() == []
        assert self.notion.page_archived is True
        approval.refresh_from_db()
        assert approval.consumed_at is None


class TestARestoreProceedsOnlyOnAPageThatSaysItIsArchived(ArchiveCase):
    def test_in_trash_without_archived_is_not_restorable(self) -> None:
        self.notion.page_in_trash = True

        for dry_run in (True, False):
            assert "Notion does not say it is archived" in self.refused_archive(dry_run=dry_run, archived=False)

        assert self.notion.page_patches == []

    def test_an_approved_restore_brings_the_page_back_verified(self) -> None:
        self.notion.page_archived = True
        self.approved(archived=False)

        payload = self.archive(dry_run=False, archived=False)

        assert (payload["outcome"], payload["verified"]) == ("restored", True)
        assert self.notion.page_patches == [{"archived": False}]
        assert self.notion.page_archived is False

    def test_a_page_that_is_already_live_is_already_restored_and_sends_no_write(self) -> None:
        payload = self.archive(dry_run=False, archived=False)

        assert payload["outcome"] == "already applied"
        assert self.mutations() == []

    def test_a_page_whose_liveness_cannot_be_proven_is_refused_for_a_restore_on_the_dry_run_and_the_write(self) -> None:
        self.make_liveness_unprovable()

        for dry_run in (True, False):
            message = self.refused_archive(dry_run=dry_run, archived=False)
            assert message.startswith("notion exit 14 (NotionPageNotLiveError): refusing to restore")
            assert "Notion does not say it is archived (liveness: unknown [absent_from_parent_database])" in message
            assert message.endswith("Nothing was written.")

        assert self.notion.page_patches == []

    def test_an_archive_of_a_page_whose_liveness_cannot_be_proven_is_refused_too(self) -> None:
        self.make_liveness_unprovable()

        for dry_run in (True, False):
            assert self.refused_archive(dry_run=dry_run).startswith("notion exit 14 (NotionPageNotLiveError)")

        assert self.notion.page_patches == []


class TestAnArchiveTheReReadDoesNotConfirmExits9(ArchiveCase):
    def test_a_restore_whose_re_read_is_unknown_exits_9(self) -> None:
        self.notion.make_database_row(database_id=_DATABASE_ID)
        self.notion.page_archived = True
        self.approved(archived=False)
        original = self.notion._patch_page

        def lose_membership(page_id: str, payload: dict[str, Any]) -> None:
            original(page_id, payload)
            self.notion.rows = [{"id": "other-row"}]

        self.monkeypatch.setattr(self.notion, "_patch_page", lose_membership)

        message = self.refused_archive(dry_run=False, archived=False)

        assert message.startswith("notion exit 9 (NotionWriteNotLandedError)")
        assert "cannot be proven" in message

    def test_an_archive_whose_re_read_is_unknown_exits_9(self) -> None:
        self.notion.make_database_row(database_id=_DATABASE_ID)
        self.approved()
        self.notion.suppress_archive = True
        original = self.notion._patch_page

        def lose_membership(page_id: str, payload: dict[str, Any]) -> None:
            original(page_id, payload)
            self.notion.rows = [{"id": "other-row"}]

        self.monkeypatch.setattr(self.notion, "_patch_page", lose_membership)

        message = self.refused_archive(dry_run=False)

        assert message.startswith("notion exit 9 (NotionWriteNotLandedError)")
        assert "cannot be proven" in message

    def test_a_trash_notion_acknowledged_but_did_not_apply_exits_9_and_keeps_the_approval_spent(self) -> None:
        approval = self.approved()
        self.notion.suppress_archive = True

        message = self.refused_archive(dry_run=False)

        assert message.startswith("notion exit 9 (NotionWriteNotLandedError)")
        assert "still reads as live, not archived" in message
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.filter(approval=approval).count() == 1

    def test_a_restore_notion_acknowledged_but_did_not_apply_exits_9(self) -> None:
        self.notion.page_archived = True
        self.approved(archived=False)
        self.notion.suppress_archive = True

        message = self.refused_archive(dry_run=False, archived=False)

        assert message.startswith("notion exit 9 (NotionWriteNotLandedError)")
        assert "still reads as archived, not live" in message


class TestTheWriteGuardBindsTheNewWrites(ArchiveCase):
    def test_an_allowed_root_cannot_be_archived(self) -> None:
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([self.notion.page_id], []))

        for dry_run in (True, False):
            assert "write root" in self.refused_archive(dry_run=dry_run)

        assert self.mutations() == []

    def test_a_non_root_ancestor_of_a_nested_write_root_cannot_be_archived(self) -> None:
        self.notion.add({"type": "child_page", "child_page": {"title": "Protected"}}, block_id=_INNER_ROOT_ID)
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([_ROOT_ID, _INNER_ROOT_ID], []))

        for dry_run in (True, False):
            message = self.refused_archive(dry_run=dry_run)
            assert f"contains the configured write root {_INNER_ROOT_ID}" in message

        assert self.mutations() == []

    def test_an_outer_write_root_containing_an_inner_write_root_cannot_be_archived(self) -> None:
        self.notion.add({"type": "child_page", "child_page": {"title": "Protected"}}, block_id=_INNER_ROOT_ID)
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([self.notion.page_id, _INNER_ROOT_ID], []))

        for dry_run in (True, False):
            assert "it is a configured write root" in self.refused_archive(dry_run=dry_run)

        assert self.mutations() == []

    def test_archive_preview_counts_child_pages(self) -> None:
        self.notion.add({"type": "child_page", "child_page": {"title": "Child"}})

        preview = self.archive(dry_run=True)

        assert preview["child_pages"] == 1

    def deny_the_page(self) -> None:
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([self.notion.page_id], [self.notion.page_id]))

    def test_a_page_under_a_denied_root_is_refused_for_an_archive_on_the_dry_run_and_on_an_approved_write(self) -> None:
        approval = self.approved()
        self.deny_the_page()

        for dry_run in (True, False):
            assert self.refused_archive(dry_run=dry_run).startswith("notion exit 17")

        approval.refresh_from_db()
        assert approval.consumed_at is None, "a refused write must not spend the approval"
        assert self.mutations() == []

    def test_a_page_under_a_denied_root_is_refused_for_a_property_set_on_the_dry_run_and_on_an_approved_write(
        self,
    ) -> None:
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Draft"}})
        args = {"page": self.notion.page_id, "name": "Status", "value": "Ready"}
        approval = self.approve(self.call("notion_property_set", **args), _PROPERTY)
        self.deny_the_page()

        for dry_run in (True, False):
            assert self.refused("notion_property_set", **args, dry_run=dry_run).startswith("notion exit 17")

        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert self.mutations() == []


class TestThePageNotionCreateMadeCanBeArchivedAndRestored(NotionWriteCase):
    def test_archive_restore_and_archive_again_leaves_the_page_archived_with_one_approval_per_step(self) -> None:
        create = {"parent": self.notion.page_id, "title": "Throwaway", "body": "A scratch page.\n", "dry_run": True}
        self.approve(self.call("notion_create", **create), "notion_create")
        page_id = self.call("notion_create", **{**create, "dry_run": False})["page_id"]

        for archived in (True, False, True):
            args = {"page": page_id, "archived": archived}
            self.approve(self.call("notion_archive", **args), _ARCHIVE if archived else _RESTORE)
            assert self.call("notion_archive", **args, dry_run=False)["verified"] is True
            assert self.notion.pages[page_id]["archived"] is archived

        assert OnBehalfAudit.objects.filter(action=_ARCHIVE).count() == 2
        assert OnBehalfAudit.objects.filter(action=_RESTORE).count() == 1


class TestPropertySet(NotionWriteCase):
    def test_a_human_edit_after_preview_is_refused_before_patch(self) -> None:
        preview = self.prop(dry_run=True)
        approval = self.approve(preview, _PROPERTY)
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Review"}})

        message = self.refused_prop(dry_run=False)

        assert message.startswith("notion exit 20 (NotionBlockChangedError)")
        assert self.notion.page_patches == []
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_a_type_change_after_preview_is_refused_before_patch(self) -> None:
        self.approve(self.prop(dry_run=True), _PROPERTY)
        self.notion.set_property("Status", {"type": "select", "select": {"name": "Draft"}})

        assert self.refused_prop(dry_run=False).startswith("notion exit 20 (NotionBlockChangedError)")
        assert self.notion.page_patches == []

    def prop(self, *, dry_run: bool, value: str = "Ready", name: str = "Status") -> dict[str, Any]:
        return self.call("notion_property_set", page=self.notion.page_id, name=name, value=value, dry_run=dry_run)

    def refused_prop(self, *, dry_run: bool, value: str = "Ready", name: str = "Status") -> str:
        return self.refused("notion_property_set", page=self.notion.page_id, name=name, value=value, dry_run=dry_run)

    def setUp(self) -> None:
        super().setUp()
        self.notion.set_property("Status", {"type": "status", "status": {"name": "Draft"}})

    def test_a_call_without_dry_run_writes_nothing_and_names_the_approval_it_needs(self) -> None:
        payload = self.call("notion_property_set", page=self.notion.page_id, name="Status", value="Ready")

        assert (payload["outcome"], payload["would"]) == ("dry_run", "set")
        assert (payload["property"], payload["type"], payload["previous"], payload["value"]) == (
            "Status",
            "status",
            "Draft",
            "Ready",
        )
        assert (payload["writing_as"]["integration"], payload["writing_as"]["bot_id"]) == ("Factory", "bot-1")
        assert payload["approval"]["action"] == _PROPERTY
        assert self.mutations() == []
        assert not OnBehalfApproval.objects.exists()

    def test_without_an_approval_the_write_is_refused_with_exit_23_and_nothing_is_sent(self) -> None:
        message = self.refused_prop(dry_run=False)

        assert message.startswith("notion exit 23 (NotionWriteNotApprovedError)")
        assert self.mutations() == []
        assert self.notion.properties["Status"]["status"] == {"name": "Draft"}

    def test_dry_run_then_record_then_write_lands_verified_spends_the_approval_and_audits_it(self) -> None:
        approval = self.approve(self.prop(dry_run=True), _PROPERTY)

        payload = self.prop(dry_run=False)

        assert (payload["outcome"], payload["previous"], payload["value"], payload["verified"]) == (
            "set",
            "Draft",
            "Ready",
            True,
        )
        assert payload["writing_as"]["bot_id"] == "bot-1"
        assert self.notion.page_patches == [{"properties": {"Status": {"status": {"name": "Ready"}}}}]
        assert self.notion.properties["Status"]["status"] == {"name": "Ready"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get().approval_id == approval.pk

    def test_the_same_call_again_is_already_applied_and_sends_no_second_write(self) -> None:
        self.approve(self.prop(dry_run=True), _PROPERTY)
        self.prop(dry_run=False)
        sent = len(self.mutations())

        payload = self.prop(dry_run=False)

        assert payload["outcome"] == "already applied"
        assert "approval" not in payload
        assert len(self.mutations()) == sent

    def test_a_permitting_posture_and_an_allowlisted_action_do_not_stand_in_for_the_approval(self) -> None:
        seed_permitting_posture()
        ConfigSetting.objects.set_value("on_behalf_auto_actions", [_PROPERTY])

        assert self.refused_prop(dry_run=False).startswith("notion exit 23")
        assert self.mutations() == []

    def test_a_write_the_re_read_does_not_confirm_exits_9(self) -> None:
        self.approve(self.prop(dry_run=True), _PROPERTY)
        self.notion.suppress_property_writes = True

        assert self.refused_prop(dry_run=False).startswith("notion exit 9 (NotionWriteNotLandedError)")

    def test_a_property_the_page_lacks_exits_12_and_a_value_the_type_cannot_hold_exits_13_on_the_dry_run(self) -> None:
        self.notion.set_property("Count", {"type": "number", "number": 3})

        assert self.refused_prop(dry_run=True, name="Missing").startswith("notion exit 12")
        assert self.refused_prop(dry_run=True, name="Count", value="many").startswith("notion exit 13")
        assert self.mutations() == []

    def test_a_dead_page_exits_14(self) -> None:
        self.notion.page_archived = True

        assert self.refused_prop(dry_run=True).startswith("notion exit 14 (NotionPageNotLiveError)")


class TestTheSurface(NotionWriteCase):
    async def _tools(self) -> dict[str, Any]:
        return {tool.name: tool for tool in await self.server.list_tools()}

    def test_the_four_tools_are_registered_with_the_annotation_their_effect_earns(self) -> None:
        tools = async_to_sync(self._tools)()

        assert tools["notion_replace"].annotations.destructive_hint is True
        assert tools["notion_create"].annotations.destructive_hint is False
        assert tools["notion_comment"].annotations.read_only_hint is False
        assert tools["notion_discussions"].annotations.read_only_hint is True

    def test_no_tool_lets_the_agent_approve_its_own_write(self) -> None:
        assert [name for name in async_to_sync(self._tools)() if "approve" in name] == []

    def test_archive_is_destructive_and_property_set_overwrites_in_place(self) -> None:
        tools = async_to_sync(self._tools)()

        assert tools["notion_archive"].annotations.destructive_hint is True
        assert tools["notion_property_set"].annotations.destructive_hint is True
        assert tools["notion_archive"].annotations.read_only_hint is False

    def test_the_server_instructions_name_both_new_tools(self) -> None:
        instructions = self.server.instructions or ""

        assert "- notion_archive(page, archived, dry_run, overlay):" in instructions
        assert "- notion_property_set(page, name, value, dry_run, overlay):" in instructions
