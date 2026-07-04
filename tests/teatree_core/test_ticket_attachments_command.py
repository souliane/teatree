"""``ticket attachments`` — inspect + fetch a ticket's attachments (PR-15, M5)."""

import tempfile
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

import teatree.backends.notion as notion_mod
from teatree.core.models import Ticket

_GITLAB = "/uploads/" + "a" * 32 + "/spec.pdf"
_NOTION = "https://www.notion.so/acme/Design-abc123"


class TestAttachmentsCommand(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="t3-teatree", extra={"branch": "1-feat"})

    def test_lists_a_missing_attachment(self) -> None:
        ticket = self._ticket()
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (
            mock.patch(
                "teatree.core.management.commands._attachment_commands.ticket_text_sources",
                return_value=[f"spec {_GITLAB}"],
            ),
            mock.patch(
                "teatree.core.management.commands._attachment_commands.attachments_dir_for",
                return_value=att_dir,
            ),
        ):
            result = cast("dict[str, object]", call_command("ticket", "attachments", str(ticket.pk)))

        assert result["missing"] == 1
        entries = cast("list[dict[str, object]]", result["entries"])
        assert entries[0]["source_url"] == _GITLAB
        assert entries[0]["fetched"] is False

    def test_zero_attachment_ticket_reports_none(self) -> None:
        ticket = self._ticket()
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (
            mock.patch(
                "teatree.core.management.commands._attachment_commands.ticket_text_sources",
                return_value=["no attachments in this issue"],
            ),
            mock.patch(
                "teatree.core.management.commands._attachment_commands.attachments_dir_for",
                return_value=att_dir,
            ),
        ):
            result = cast("dict[str, object]", call_command("ticket", "attachments", str(ticket.pk)))

        assert result["missing"] == 0
        assert result["entries"] == []

    def test_fetch_downloads_and_clears_missing(self) -> None:
        ticket = self._ticket()
        att_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def _fake_download(*, url: str, dest: Path) -> Path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"notion-bytes")
            return dest

        with (
            mock.patch(
                "teatree.core.management.commands._attachment_commands.ticket_text_sources",
                return_value=[f"mock {_NOTION}"],
            ),
            mock.patch(
                "teatree.core.management.commands._attachment_commands.attachments_dir_for",
                return_value=att_dir,
            ),
            mock.patch.object(notion_mod, "download_notion_file", _fake_download),
        ):
            result = cast("dict[str, object]", call_command("ticket", "attachments", str(ticket.pk), fetch=True))

        assert result["missing"] == 0
        entries = cast("list[dict[str, object]]", result["entries"])
        assert entries[0]["fetched"] is True

    def test_missing_ticket_exits(self) -> None:
        with pytest.raises(SystemExit):
            call_command("ticket", "attachments", "999999")
