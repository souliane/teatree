"""Which ticket a workspace-scoped command acts on.

The two anchors are the point: ``provision`` / ``start`` / ``ready`` / ``teardown``
act on every worktree in a ticket, so the caller may stand inside a repo worktree
OR at the workspace root holding those repo subdirs, and both must reach the same
ticket. A path owned by neither must RAISE rather than resolve to something.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.intake.resolve import WorktreeNotFoundError
from teatree.core.management.commands._workspace.anchor import resolve_workspace_ticket
from teatree.core.models import Ticket

_MODULE = "teatree.core.management.commands._workspace.anchor"


class TestResolveWorkspaceTicket(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3872")
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_repo_worktree_resolves_through_its_own_row(self) -> None:
        anchor = type("Anchor", (), {"ticket": self.ticket})()
        with patch(f"{_MODULE}.resolve_worktree", return_value=anchor):
            assert resolve_workspace_ticket(str(self.workspace)) == self.ticket

    def test_the_workspace_root_falls_back_to_its_owning_ticket(self) -> None:
        with (
            patch(f"{_MODULE}.resolve_worktree", side_effect=WorktreeNotFoundError("not a worktree")),
            patch(f"{_MODULE}.workspace_owner_ticket", return_value=self.ticket) as owner,
        ):
            assert resolve_workspace_ticket(str(self.workspace)) == self.ticket

        assert owner.call_args.args[0] == self.workspace.resolve()

    def test_an_unowned_path_raises_rather_than_guessing(self) -> None:
        with (
            patch(f"{_MODULE}.resolve_worktree", side_effect=WorktreeNotFoundError("not a worktree")),
            patch(f"{_MODULE}.workspace_owner_ticket", return_value=None),
            pytest.raises(WorktreeNotFoundError),
        ):
            resolve_workspace_ticket(str(self.workspace))

    def test_an_empty_path_anchors_on_the_callers_cwd(self) -> None:
        with (
            patch(f"{_MODULE}.resolve_worktree", side_effect=WorktreeNotFoundError("not a worktree")),
            patch(f"{_MODULE}._get_user_cwd", return_value=str(self.workspace)),
            patch(f"{_MODULE}.workspace_owner_ticket", return_value=self.ticket) as owner,
        ):
            assert resolve_workspace_ticket("") == self.ticket

        assert owner.call_args.args[0] == self.workspace.resolve()
