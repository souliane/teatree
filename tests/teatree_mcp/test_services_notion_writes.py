"""The Notion WRITE tools: dry run by default, a recorded approval for the real write, every exit code kept.

Driven through ``MCPServer.call_tool`` against ``FakeNotion`` — the HTTP double is the only fake, so the
write guard, the verify-after-write, the outbound-claim ledger and the approval rows all run for real.
"""

import json
from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.cli.notion_mcp_seam import register
from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit, OutboundClaim
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp import services_notion
from teatree.mcp.server import build_server
from tests.teatree_backends.notion._fake_notion import install_fake_notion
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture
from tests.teatree_mcp._call_tool_result import structured

_ROOTS = "teatree.backends.notion.write_guard.notion_write_roots"
_OWNER = "alice.example"
_REPLACE = "notion_replace"


class _NotionOverlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.NOTION}))
        self.connectors = OverlayConnectors()


def _run(text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": text, "link": None},
        "plain_text": text,
        "href": None,
        "annotations": {"bold": False, "italic": False, "code": False, "color": "default"},
    }


class NotionWriteCase(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTION_TOKEN", "test-token")
        monkeypatch.setenv("T3_NOTION_HTTP_MAX_RETRIES", "0")
        self.notion = install_fake_notion(monkeypatch)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(services_notion._factory_registry, "factory", services_notion._factory_registry.factory)
        register()
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _NotionOverlay()}):
            self.server = build_server()

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return structured(async_to_sync(self.server.call_tool)(tool, args))

    def refused(self, tool: str, **args: Any) -> str:
        with pytest.raises(ToolError) as caught:
            self.call(tool, **args)
        return str(caught.value).removeprefix(f"Error executing tool {tool}: ")

    def paragraph(self, text: str) -> str:
        return self.notion.add({"type": "paragraph", "paragraph": {"rich_text": [_run(text)]}})

    def approve(self, payload: dict[str, Any], action: str = _REPLACE) -> OnBehalfApproval:
        return OnBehalfApproval.record(payload["approval"]["target"], action, _OWNER)

    def mutations(self) -> list[tuple[str, str]]:
        return [request for request in self.notion.requests if request[0] != "GET"]


class TestTheDefaultIsADryRun(NotionWriteCase):
    def test_a_call_without_dry_run_writes_nothing_and_names_the_approval_it_needs(self) -> None:
        block = self.paragraph("Rates reset every quarter.")

        payload = self.call("notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly")

        assert payload["outcome"] == "dry_run"
        assert payload["block_id"] == block
        assert "-Rates reset every quarter." in payload["diff"]
        assert "+Rates reset monthly." in payload["diff"]
        assert payload["approval"]["action"] == _REPLACE
        assert payload["approval"]["target"].startswith(f"notion:{self.notion.page_id}#{block}:")
        assert self.mutations() == []
        assert not OutboundClaim.objects.exists()
        assert not OnBehalfApproval.objects.exists()

    def test_the_dry_run_hands_back_the_command_that_records_the_approval(self) -> None:
        self.paragraph("Rates reset every quarter.")

        approval = self.call("notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly")[
            "approval"
        ]

        assert approval["record_command"].startswith("t3 review approve-on-behalf 'notion:")
        assert approval["record_command"].endswith(f" {_REPLACE} --approver <owner-id>")


class TestARealWriteNeedsARecordedApproval(NotionWriteCase):
    def replace(self, *, dry_run: bool, new: str = "every quarter, per the bank's mail") -> dict[str, Any]:
        return self.call(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text=new, dry_run=dry_run
        )

    def test_without_one_the_write_is_refused_with_exit_23_and_nothing_is_sent(self) -> None:
        block = self.paragraph("Rates reset every quarter.")

        message = self.refused(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly", dry_run=False
        )

        assert message.startswith("notion exit 23 (NotionWriteNotApprovedError)")
        assert "approve-on-behalf" in message
        assert self.mutations() == []
        assert self.notion.text_of(block) == "Rates reset every quarter."

    def test_dry_run_then_record_then_write_lands_verified_spends_the_approval_and_audits_it(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        approval = self.approve(self.replace(dry_run=True))

        payload = self.replace(dry_run=False)

        assert payload["outcome"] == "replaced"
        assert payload["verified"] is True
        assert payload["writing_as"]["bot_id"] == "bot-1"
        assert self.notion.text_of(block) == "Rates reset every quarter, per the bank's mail."
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get().approval_id == approval.pk
        assert OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT).verified_at is not None

    def test_the_same_call_again_is_already_applied_and_sends_no_second_write(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.approve(self.replace(dry_run=True))
        self.replace(dry_run=False)
        sent = len(self.mutations())

        payload = self.replace(dry_run=False)

        assert payload["outcome"] == "already applied"
        assert "approval" not in payload
        assert len(self.mutations()) == sent

    def test_an_approval_is_single_use(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.approve(self.replace(dry_run=True))
        self.replace(dry_run=False)
        self.notion.blocks[block]["paragraph"]["rich_text"] = [_run("Rates reset every quarter.")]

        assert self.refused(
            "notion_replace",
            page=self.notion.page_id,
            old_text="every quarter",
            new_text="every quarter, per the bank's mail",
            dry_run=False,
        ).startswith("notion exit 23")

    def test_a_block_edited_after_the_dry_run_is_refused_not_overwritten(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.approve(self.replace(dry_run=True, new="monthly"))
        edited = "Rates reset every quarter, per the bank's mail."
        self.notion.blocks[block]["paragraph"]["rich_text"] = [_run(edited)]

        message = self.refused(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly", dry_run=False
        )

        assert message.startswith("notion exit 20 (NotionBlockChangedError)")
        assert "different text" in message
        assert self.mutations() == []
        assert self.notion.text_of(block) == edited

    def test_a_permitting_posture_and_an_allowlisted_action_do_not_stand_in_for_the_approval(self) -> None:
        self.paragraph("Rates reset every quarter.")
        seed_permitting_posture()
        ConfigSetting.objects.set_value("on_behalf_auto_actions", [_REPLACE])

        message = self.refused(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly", dry_run=False
        )

        assert message.startswith("notion exit 23")
        assert self.mutations() == []


class TestTheWriteGuardBindsBothModes(NotionWriteCase):
    def test_a_page_under_a_denied_root_is_refused_on_the_dry_run_and_on_an_approved_write(self) -> None:
        self.paragraph("Rates reset every quarter.")
        args = {"page": self.notion.page_id, "old_text": "every quarter", "new_text": "monthly"}
        approval = self.approve(self.call("notion_replace", **args))
        self.monkeypatch.setattr(_ROOTS, lambda _overlay: ([self.notion.page_id], [self.notion.page_id]))

        for dry_run in (True, False):
            assert self.refused("notion_replace", **args, dry_run=dry_run).startswith("notion exit 17")

        approval.refresh_from_db()
        assert approval.consumed_at is None, "a refused write must not spend the approval"
        assert self.mutations() == []


class TestEveryFailureKeepsItsOwnExitCode(NotionWriteCase):
    def write(self, *, old: str = "every quarter") -> str:
        args = {"page": self.notion.page_id, "old_text": old, "new_text": "monthly"}
        self.approve(self.call("notion_replace", **args))
        return self.refused("notion_replace", **args, dry_run=False)

    def test_an_anchor_that_occurs_twice_exits_19(self) -> None:
        self.paragraph("every quarter")
        self.paragraph("and every quarter again")

        assert self.refused(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly"
        ).startswith("notion exit 19 (NotionAnchorError)")

    def test_a_block_edited_after_it_was_read_exits_20_before_the_approval_is_spent(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        args = {"page": self.notion.page_id, "old_text": "every quarter", "new_text": "monthly"}
        approval = self.approve(self.call("notion_replace", **args))
        self.notion.edit_on_read[block] = "Rates reset every quarter, per the bank's mail."

        message = self.refused("notion_replace", **args, dry_run=False)

        assert message.startswith("notion exit 20 (NotionBlockChangedError)")
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_a_write_the_re_read_does_not_confirm_exits_9(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.notion.suppress_block_updates = True

        assert self.write().startswith("notion exit 9 (NotionWriteNotLandedError)")

    def test_a_write_whose_response_failed_after_it_applied_exits_22(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.notion.update_then_fail = (502, "bad_gateway")

        assert self.write().startswith("notion exit 22 (NotionWriteUnverifiedError)")

    def test_a_block_no_longer_shared_exits_6(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.notion.refuse_update = (404, "object_not_found")

        assert self.write().startswith("notion exit 6 (NotionNotSharedError)")

    def test_a_dead_page_exits_14(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.notion.page_archived = True

        assert self.refused(
            "notion_replace", page=self.notion.page_id, old_text="every quarter", new_text="monthly"
        ).startswith("notion exit 14 (NotionPageNotLiveError)")

    def test_input_the_cli_refuses_exits_1(self) -> None:
        self.paragraph("Rates reset every quarter.")

        for old, new in (("", "x"), ("every quarter", "every quarter")):
            assert self.refused("notion_replace", page=self.notion.page_id, old_text=old, new_text=new).startswith(
                "notion exit 1 (ValueError)"
            )
        assert self.notion.requests == []


class TestAWriteThatMayHaveLandedStaysSpentAndRecorded(NotionWriteCase):
    def test_exit_22_keeps_the_approval_spent_the_ledger_unverified_and_the_audit_written(self) -> None:
        self.paragraph("Rates reset every quarter.")
        self.notion.update_then_fail = (502, "bad_gateway")
        args = {"page": self.notion.page_id, "old_text": "every quarter", "new_text": "monthly"}
        approval = self.approve(self.call("notion_replace", **args))

        assert self.refused("notion_replace", **args, dry_run=False).startswith("notion exit 22")

        approval.refresh_from_db()
        assert approval.consumed_at is not None
        claim = OutboundClaim.objects.get(kind=OutboundClaim.Kind.NOTION_EDIT)
        assert claim.extra["outcome"] == "unverified"
        assert claim.drift_detected is True
        assert OnBehalfAudit.objects.filter(approval=approval).count() == 1
        assert "Rates reset" not in json.dumps(claim.extra)


class TestCreate(NotionWriteCase):
    _BODY = "## Scope\n\nThe factory owns the build, never the request path.\n"

    def create(self, *, dry_run: bool, title: str = "CTO brief") -> dict[str, Any]:
        return self.call(
            "notion_create", parent=self.notion.page_id, title=title, body=self._BODY, icon="🏗️", dry_run=dry_run
        )

    def test_the_dry_run_writes_nothing_and_names_the_approval(self) -> None:
        payload = self.create(dry_run=True)

        assert (payload["outcome"], payload["would"], payload["blocks"]) == ("dry_run", "create", 2)
        assert payload["approval"]["action"] == "notion_create"
        assert ("POST", "/pages") not in self.notion.requests
        assert self.notion.pages == {}

    def test_a_page_already_carrying_the_title_is_reported_without_asking_for_an_approval(self) -> None:
        self.approve(self.create(dry_run=True), "notion_create")
        self.create(dry_run=False)

        payload = self.create(dry_run=True)

        assert (payload["outcome"], payload["would"]) == ("dry_run", "exists")
        assert "approval" not in payload

    def test_an_approved_create_verifies_the_title_and_body(self) -> None:
        self.approve(self.create(dry_run=True), "notion_create")

        payload = self.create(dry_run=False)

        assert (payload["outcome"], payload["verified"]) == ("created", True)
        assert self.notion.body_texts(payload["page_id"]) == [
            "Scope",
            "The factory owns the build, never the request path.",
        ]
        assert self.notion.pages[payload["page_id"]]["icon"] == {"type": "emoji", "emoji": "🏗️"}

    def test_an_unapproved_create_is_refused_and_posts_nothing(self) -> None:
        assert self.refused(
            "notion_create", parent=self.notion.page_id, title="CTO brief", body=self._BODY, dry_run=False
        ).startswith("notion exit 23")
        assert ("POST", "/pages") not in self.notion.requests

    def test_an_approval_recorded_for_another_title_does_not_cover_this_one(self) -> None:
        self.approve(self.create(dry_run=True, title="Other brief"), "notion_create")

        assert self.refused(
            "notion_create", parent=self.notion.page_id, title="CTO brief", body=self._BODY, dry_run=False
        ).startswith("notion exit 20")

    def test_a_blank_title_or_body_exits_1(self) -> None:
        for title, body in (("  ", self._BODY), ("CTO brief", "\n")):
            assert self.refused("notion_create", parent=self.notion.page_id, title=title, body=body).startswith(
                "notion exit 1"
            )


class TestComment(NotionWriteCase):
    _MARKER = "[t3:prd-agent]"
    _BODY = "[t3:prd-agent] PRD refreshed."

    def comment(self, *, dry_run: bool, **extra: str) -> dict[str, Any]:
        return self.call(
            "notion_comment",
            page=self.notion.page_id,
            body=self._BODY,
            marker=self._MARKER,
            dry_run=dry_run,
            **extra,
        )

    def test_a_page_comment_is_approved_posted_and_verified(self) -> None:
        preview = self.comment(dry_run=True)
        assert (preview["outcome"], preview["would"]) == ("dry_run", "post")
        assert self.notion.comments == []
        self.approve(preview, "notion_comment")

        payload = self.comment(dry_run=False)

        assert (payload["outcome"], payload["verified"]) == ("posted", True)
        assert self.notion.comment_texts() == [self._BODY]

    def test_a_quote_anchors_the_comment_on_the_one_block_holding_it(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.approve(self.comment(dry_run=True, quote="every quarter"), "notion_comment")

        payload = self.comment(dry_run=False, quote="every quarter")

        assert payload["anchor_id"] == block
        assert self.notion.comment_texts() == [f"“every quarter”\n\n{self._BODY}"]

    def test_a_discussion_is_replied_to_where_it_is_anchored(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.notion.comment_on(block, "please confirm", discussion_id="disc-1")
        self.approve(self.comment(dry_run=True, discussion="disc-1"), "notion_comment")

        payload = self.comment(dry_run=False, discussion="disc-1")

        assert (payload["anchor_id"], payload["discussion_id"]) == (block, "disc-1")
        assert self.notion.comments[-1]["discussion_id"] == "disc-1"

    def test_a_marker_already_on_the_page_is_a_duplicate_before_any_approval_is_asked_for(self) -> None:
        self.notion.comments.append(
            {
                "object": "comment",
                "id": "comment-seed",
                "discussion_id": "disc-seed",
                "parent": {"type": "page_id", "page_id": self.notion.page_id},
                "rich_text": [_run(f"{self._MARKER} an earlier run")],
            }
        )

        preview = self.comment(dry_run=True)
        written = self.comment(dry_run=False)

        assert (preview["outcome"], preview["would"], "approval" in preview) == ("dry_run", "duplicate", False)
        assert (written["outcome"], written["comment_id"]) == ("duplicate", "comment-seed")
        assert ("POST", "/comments") not in self.notion.requests

    def test_a_quote_together_with_a_discussion_exits_1(self) -> None:
        self.paragraph("Rates reset every quarter.")

        message = self.refused(
            "notion_comment", page=self.notion.page_id, body="x", quote="every quarter", discussion="disc-1"
        )

        assert message.startswith("notion exit 1")

    def test_an_empty_body_exits_1_rather_than_raising(self) -> None:
        assert self.refused("notion_comment", page=self.notion.page_id, body="  ").startswith("notion exit 1")

    def test_a_discussion_the_page_does_not_hold_exits_21(self) -> None:
        assert self.refused("notion_comment", page=self.notion.page_id, body="x", discussion="nope").startswith(
            "notion exit 21"
        )


class TestDiscussions(NotionWriteCase):
    def test_the_enumeration_is_returned_complete(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.notion.comment_on(block, "please confirm", discussion_id="disc-1")

        payload = self.call("notion_discussions", page=self.notion.page_id)

        assert payload["complete"] is True
        assert [comment["discussion_id"] for comment in payload["comments"]] == ["disc-1"]

    def test_an_unreadable_block_exits_18_naming_it_rather_than_returning_the_partial_list(self) -> None:
        block = self.paragraph("Rates reset every quarter.")
        self.notion.comments_fail_for[block] = (403, "restricted_resource")

        message = self.refused("notion_discussions", page=self.notion.page_id)

        assert message.startswith("notion exit 18 (NotionIncompleteEnumerationError)")
        assert block in message
