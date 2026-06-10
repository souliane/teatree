"""``workspace ticket`` intake renders ``Ticket.context`` collapsed (#627).

The collapsed/truncated formatting is a pure function (``render_ticket_context``)
so it is unit-tested directly — the "pure logic" exception in the
Test-Writing Doctrine. The wiring (intake calls the renderer with the
ticket's stored context) is verified through the real ``workspace ticket``
command. ``call_command`` is *not* given a ``stdout=`` override here: doing
so makes Django's ``BaseCommand.execute`` write the command's bare ``int``
return through the wrapper (``'int' has no attribute 'endswith'`` — a
pre-existing django-typer/Django interaction the sibling workspace tests
also avoid). The render lines are asserted on the command instance's own
captured ``stdout`` instead.
"""

import os
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.workspace as workspace_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Ticket
from teatree.core.models.ticket_display import render_ticket_context
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class RenderTicketContextTest(TestCase):
    def test_empty_context_renders_nothing(self) -> None:
        assert render_ticket_context("") == ""
        assert render_ticket_context("   \n  ") == ""

    def test_present_context_renders_collapsed_block(self) -> None:
        text = render_ticket_context("[2026-05-18 09:00] dev_lr_id = 5842")
        assert text.startswith("\n\n")
        assert "<details>" in text
        assert "<summary>Ticket context (durable knowledge store)</summary>" in text
        assert "dev_lr_id = 5842" in text
        assert "</details>" in text
        assert "truncated" not in text

    def test_long_context_is_truncated_with_pointer(self) -> None:
        body = "\n".join(f"[2026-05-18 09:{i:02d}] key{i} = v{i}" for i in range(60))
        text = render_ticket_context(body, max_lines=40)
        assert "<details>" in text
        assert "key0 = v0" in text
        assert "key59 = v59" not in text
        assert "20 more line(s) truncated" in text
        assert "ticket context show" in text


class WorkspaceTicketContextWiringTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        mock_result = MagicMock(returncode=0, stdout="dev", stderr="")
        self.enterContext(patch.object(utils_run_mod.subprocess, "run", return_value=mock_result))
        workspace = Path(os.environ["HOME"]) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for repo in ("backend", "frontend"):
            (workspace / repo / ".git").mkdir(parents=True, exist_ok=True)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_intake_renders_stored_context_collapsed(self) -> None:
        Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/600",
            context="\n\n[2026-05-18 09:00] dev_lr_id = 5842 (used by Wouter for Round 2)",
        )
        out = StringIO()
        with patch.object(workspace_mod.Command, "print_result", new=False, create=True):
            cmd = workspace_mod.Command(stdout=out)
            call_command(cmd, "ticket", "https://example.com/issues/600")
        text = out.getvalue()
        assert "<details>" in text
        assert "Ticket context (durable knowledge store)" in text
        assert "dev_lr_id = 5842" in text

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_intake_omits_section_when_no_context(self) -> None:
        out = StringIO()
        with patch.object(workspace_mod.Command, "print_result", new=False, create=True):
            cmd = workspace_mod.Command(stdout=out)
            call_command(cmd, "ticket", "https://example.com/issues/602")
        text = out.getvalue()
        ticket = Ticket.objects.get(issue_url="https://example.com/issues/602")
        assert ticket.context == ""
        assert "<details>" not in text
        assert "Ticket context" not in text
