"""``pr create`` MR title/description convention gate (#1540).

Integration coverage proving the deterministic gate runs through the REAL
``validate_pr_metadata`` -> overlay ``validate_pr`` path BEFORE the FSM
advances to SHIPPED (so before the gh/glab create network call). The title is
derived from the commit subject and the description from subject+body, so the
test drives both by patching only ``git.last_commit_message`` — an unstoppable
external. The default ``CommandOverlay`` uses the default ``OverlayMetadata``,
so the gate it exercises is the one every overlay inherits.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Ticket
from teatree.core.mr_metadata import DEFAULT_MR_TITLE_REGEX

from ._shared import _MOCK_OVERLAY, _shippable_ticket


class TestPrCreateMrTitleGate(TestCase):
    def _run(self, subject: str, body: str) -> dict[str, object]:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command.git, "current_branch", return_value="feature-branch"),
            patch.object(_pr_preview.git, "last_commit_message", return_value=(subject, body)),
        ):
            return cast("dict[str, object]", call_command("pr", "create", str(self._ticket.id)))

    def test_non_conforming_title_is_rejected_before_shipping(self) -> None:
        self._ticket = _shippable_ticket()
        result = self._run("Add the gate", "## What\nx\n\n## Why\ny")

        self._ticket.refresh_from_db()
        assert self._ticket.state == Ticket.State.REVIEWED  # never advanced
        assert result["error"] == "PR validation failed"
        details = cast("list[str]", result["details"])
        assert any(DEFAULT_MR_TITLE_REGEX in d for d in details)

    def test_missing_what_why_body_is_scaffolded_by_the_generator(self) -> None:
        # #312: the generator emits the standard What/Why body by default, so a
        # conforming title with a flat-paragraph body no longer trips the gate —
        # the scaffold is added and the MR ships. (The What/Why gate still
        # protects an explicitly-authored description that bypasses the
        # generator; the commit-body path is now self-healing.)
        self._ticket = _shippable_ticket()
        result = self._run("feat(ship): add the gate (#1540)", "Just a flat paragraph.")

        self._ticket.refresh_from_db()
        assert self._ticket.state == Ticket.State.SHIPPED
        assert "error" not in result

    def test_conforming_title_and_what_why_passes(self) -> None:
        self._ticket = _shippable_ticket()
        result = self._run("feat(ship): add the gate (#1540)", "## What\nAdds it.\n\n## Why\nMissed often.")

        self._ticket.refresh_from_db()
        assert self._ticket.state == Ticket.State.SHIPPED
        assert "error" not in result
