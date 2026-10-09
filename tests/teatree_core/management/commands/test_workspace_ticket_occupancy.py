"""``workspace ticket`` refuses to hand back an occupied checkout (souliane/teatree#3952).

Re-resolving a ticket whose worktree already exists is the operator lane's handout
seam: before this guard it printed the path of a tree another agent was live in.
The upstream of the command (overlay resolution, intake, provisioning) is stubbed
— the assertion is only about the new branch: an occupied checkout short-circuits
BEFORE provisioning, and ``--take-over`` is the explicit override.
"""

import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

import teatree.core.management.commands.workspace as workspace_mod
from teatree.core.worktree.occupancy import acquire, occupancy_holder
from tests.factories import TicketFactory, WorktreeFactory


class _TicketHandoutCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = TicketFactory()
        self.checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: self.checkout.exists() and self.checkout.rmdir())
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        self.finalize = MagicMock(return_value=int(self.ticket.pk))
        self.enterContext(patch.object(workspace_mod._wh, "warn_orphans"))
        self.enterContext(patch.object(workspace_mod, "get_overlay", return_value=MagicMock()))
        self.enterContext(patch.object(workspace_mod, "canonicalize_issue_ref", side_effect=lambda _o, url: url))
        self.enterContext(patch.object(workspace_mod, "resolve_adopt_context", return_value=None))
        self.enterContext(patch.object(workspace_mod, "adopt_preflight_refusal", return_value=None))
        self.enterContext(patch.object(workspace_mod, "build_intake", return_value=MagicMock()))
        self.enterContext(patch.object(workspace_mod, "build_ticket", return_value=self.ticket))
        self.enterContext(patch.object(workspace_mod, "finalize_ticket_provision", self.finalize))

    def run_ticket(self, *extra: str) -> str:
        err = StringIO()
        call_command("workspace", "ticket", "https://example.com/issues/42", *extra, stderr=err)
        return err.getvalue()

    def run_refused_ticket(self) -> str:
        err = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("workspace", "ticket", "https://example.com/issues/42", stderr=err)
        assert exc.value.code == 1
        return err.getvalue()


class OccupiedHandoutTests(_TicketHandoutCase):
    def test_an_occupied_checkout_is_refused_naming_the_holder(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="factory-lane")

        err = self.run_refused_ticket()

        assert "task:7" in err
        assert "factory-lane" in err
        assert "--take-over" in err

    def test_the_refusal_happens_before_provisioning(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="factory-lane")

        self.run_refused_ticket()

        self.finalize.assert_not_called()

    def test_take_over_proceeds_and_leaves_the_incumbents_claim_alone(self) -> None:
        acquire(self.worktree, holder="task:7", holder_session="factory-lane")

        self.run_ticket("--take-over")

        self.finalize.assert_called_once()
        held = occupancy_holder(self.worktree.__class__.objects.get(pk=self.worktree.pk))
        assert held is not None
        assert held.holder == "task:7"

    def test_a_free_checkout_provisions_as_before(self) -> None:
        self.run_ticket()

        self.finalize.assert_called_once()
