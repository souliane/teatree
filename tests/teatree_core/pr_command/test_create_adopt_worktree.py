"""``pr create --adopt-worktree`` — follow-up PR on a terminal ticket (#3327)."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
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


class TestAdoptResolvesTheInvocationCwd(TestCase):
    """Adoption reads where the OPERATOR stood, not the process cwd (#4281).

    Under ``deploy/t3`` the process starts in the image WORKDIR, so a hardcoded
    ``cwd="."`` can never reach a worktree — every containerized adopt refused,
    and blamed the directory rather than the lost propagation.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp = tmp_path
        self._monkeypatch = monkeypatch

    def _merged_ticket_ready_to_ship(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.MERGED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        return ticket

    def _linked_worktree(self, name: str = "teatree") -> Path:
        wt = self._tmp / name
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /clone/.git/worktrees/teatree\n")
        return wt

    def _plain_dir(self, name: str) -> Path:
        plain = self._tmp / name
        plain.mkdir()
        return plain

    def _create(self, ticket: Ticket) -> dict[str, object]:
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="4281-followup"),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", MagicMock()),
        ):
            return cast(
                "dict[str, object]",
                pr_command.Command().create(str(ticket.id), adopt_worktree=True),
            )

    def test_adopts_the_declared_invocation_cwd_not_the_process_cwd(self) -> None:
        ticket = self._merged_ticket_ready_to_ship()
        worktree_dir = self._linked_worktree()
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(worktree_dir))
        self._monkeypatch.chdir(self._plain_dir("image-workdir"))

        result = self._create(ticket)

        assert result.get("queued") is True, result
        assert ticket.worktrees.get().extra["worktree_path"] == str(worktree_dir.resolve())

    def test_undeclared_invocation_cwd_names_the_propagation_failure(self) -> None:
        ticket = self._merged_ticket_ready_to_ship()
        self._monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)
        image_workdir = self._plain_dir("image-workdir")
        self._monkeypatch.chdir(image_workdir)

        error = str(self._create(ticket)["error"])

        assert INVOCATION_CWD_ENV in error
        assert str(image_workdir.resolve()) in error
        assert not ticket.worktrees.exists()

    def test_declared_non_worktree_keeps_the_plain_refusal(self) -> None:
        # The operator really did stand outside a worktree — blaming propagation
        # here would send them after an env var that is doing its job.
        ticket = self._merged_ticket_ready_to_ship()
        stood_here = self._plain_dir("not-a-worktree")
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(stood_here))
        self._monkeypatch.chdir(self._plain_dir("image-workdir"))

        error = str(self._create(ticket)["error"])

        assert error.startswith(f"Refusing to adopt {stood_here.resolve()}: not a git worktree")
        assert INVOCATION_CWD_ENV not in error
