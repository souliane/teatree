"""``pr create --adopt-worktree`` — follow-up PR on a terminal ticket (#3327)."""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.provision.worktree_adopt import WorktreeAdoptError

from ._shared import _MOCK_OVERLAY


class TestWorktreeMissingMessage(TestCase):
    """The refusal names the follow-up recovery only when it actually applies."""

    def test_terminal_ticket_missing_row_names_adopt_recovery(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", pr_command.Command().create(str(ticket.id)))

        assert "--adopt-worktree" in str(result["error"])

    def test_never_provisioned_ticket_gets_plain_refusal(self) -> None:
        # A pre-review ticket that was simply never provisioned should be
        # provisioned, not adopted — no --adopt-worktree advice.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", pr_command.Command().create(str(ticket.id)))

        assert result == {"error": "ticket has no worktree"}


class TestAdoptWorktreeFlow(TestCase):
    """--adopt-worktree attaches a row, reopens the terminal FSM, and ships."""

    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def _merged_ticket_ready_to_ship(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        return ticket

    def _adopt_side_effect(self, ticket: Ticket):
        def _create(_ticket: Ticket, *, cwd: str) -> Worktree:
            _ = cwd
            return Worktree.objects.create(
                ticket=_ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="4321-followup",
                extra={"worktree_path": "/tmp/backend"},
            )

        return _create

    def test_adopts_then_reopens_and_ships(self) -> None:
        ticket = self._merged_ticket_ready_to_ship()
        adopt = MagicMock(side_effect=self._adopt_side_effect(ticket))

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands._pr_worktree.adopt_worktree_for_ticket", adopt),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", MagicMock()),
        ):
            result = cast(
                "dict[str, object]",
                pr_command.Command().create(str(ticket.id), adopt_worktree=True),
            )

        adopt.assert_called_once()
        assert result.get("queued") is True
        ticket.refresh_from_db()
        # MERGED → (reopen_for_followup) REVIEWED → (ship) SHIPPED.
        assert ticket.state == Ticket.State.SHIPPED
        assert ticket.worktrees.count() == 1

    def test_adopt_guardrail_failure_surfaces_as_worktree_missing(self) -> None:
        ticket = self._merged_ticket_ready_to_ship()
        adopt = MagicMock(side_effect=WorktreeAdoptError("not a git worktree here"))

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands._pr_worktree.adopt_worktree_for_ticket", adopt),
        ):
            result = cast(
                "dict[str, object]",
                pr_command.Command().create(str(ticket.id), adopt_worktree=True),
            )

        assert result == {"error": "not a git worktree here"}
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED  # no FSM advance on refusal
