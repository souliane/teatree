"""``tasks start``'s terminal-launch concern, split out of the command module."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

import teatree.core.management.commands.tasks_interactive_launch as launch_mod
from teatree.core.management.commands.tasks_interactive_launch import build_claude_command, exec_inline
from teatree.core.models import Session, Task, Ticket

_SESSION_UUID = "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"


class TestBuildClaudeCommand(TestCase):
    def _make_task(self, *, agent_id: str) -> Task:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id=agent_id)
        return Task.objects.create(ticket=ticket, session=session, phase="coding")

    def test_a_uuid_session_resumes_it(self) -> None:
        task = self._make_task(agent_id=_SESSION_UUID)
        with patch.object(launch_mod.shutil, "which", return_value="/usr/bin/claude"):
            assert build_claude_command(task) == ["/usr/bin/claude", "--resume", _SESSION_UUID]

    def test_a_missing_claude_binary_raises(self) -> None:
        task = self._make_task(agent_id=_SESSION_UUID)
        with patch.object(launch_mod.shutil, "which", return_value=None), pytest.raises(FileNotFoundError):
            build_claude_command(task)


class TestExecInline:
    def test_it_exits_with_the_child_return_code(self) -> None:
        run = MagicMock(return_value=3)
        with patch("teatree.utils.run.run_streamed", new=run), pytest.raises(SystemExit) as exc:
            exec_inline(["/usr/bin/claude"])

        assert exc.value.code == 3
        run.assert_called_once()
