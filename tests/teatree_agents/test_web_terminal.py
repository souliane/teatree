"""Tests for teatree.agents.web_terminal — ttyd session launching."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.agents.web_terminal import (
    _find_free_port,
    _get_resume_session_id,
    launch_web_session,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket

_WHICH = "teatree.agents.web_terminal.shutil.which"
_FREE_PORT = "teatree.agents.web_terminal._find_free_port"
_POPEN = "teatree.agents.web_terminal.subprocess.Popen"

# --- _find_free_port ---


def test_find_free_port_returns_int() -> None:
    port = _find_free_port()
    assert isinstance(port, int)
    assert port > 0


# --- _get_resume_session_id ---


class TestGetResumeSessionId(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_with_uuid_agent_id(self) -> None:
        session = Session.objects.create(
            ticket=self.ticket,
            agent_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        )
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert _get_resume_session_id(task) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_non_uuid_agent_id(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert _get_resume_session_id(task) == ""

    def test_empty_agent_id(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="")
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert _get_resume_session_id(task) == ""


# --- launch_web_session ---


class TestLaunchWebSession(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_creates_attempt(self) -> None:
        with (
            patch(_WHICH, side_effect=lambda name: f"/usr/bin/{name}"),
            patch(_FREE_PORT, return_value=8888),
            patch(_POPEN, new_callable=MagicMock),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = launch_web_session(task, phase="coding", overlay_skill_metadata={})

            assert attempt.launch_url == "http://127.0.0.1:8888"
            assert TaskAttempt.objects.count() == 1

    def test_raises_when_claude_missing(self) -> None:
        with (
            patch(_WHICH, return_value=None),
            patch(_FREE_PORT, return_value=8888),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            with pytest.raises(FileNotFoundError, match="claude CLI is not installed"):
                launch_web_session(task, phase="coding", overlay_skill_metadata={})

    def test_raises_when_ttyd_missing(self) -> None:
        def which_mock(name: str) -> str | None:
            return "/usr/bin/claude" if name == "claude" else None

        with (
            patch(_WHICH, side_effect=which_mock),
            patch(_FREE_PORT, return_value=8888),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            with pytest.raises(FileNotFoundError, match="ttyd is not installed"):
                launch_web_session(task, phase="coding", overlay_skill_metadata={})

    def test_resumes_session(self) -> None:
        with (
            patch(_WHICH, side_effect=lambda name: f"/usr/bin/{name}"),
            patch(_FREE_PORT, return_value=7777),
            patch(_POPEN, new_callable=MagicMock) as popen_mock,
        ):
            session = Session.objects.create(
                ticket=self.ticket,
                agent_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = launch_web_session(task, phase="coding", overlay_skill_metadata={})

            assert attempt.launch_url == "http://127.0.0.1:7777"

            # Verify --resume was passed
            call_args = popen_mock.call_args[0][0]
            assert "--resume" in call_args
            resume_idx = call_args.index("--resume")
            assert call_args[resume_idx + 1] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_new_session_uses_system_context(self) -> None:
        with (
            patch(_WHICH, side_effect=lambda name: f"/usr/bin/{name}"),
            patch(_FREE_PORT, return_value=7777),
            patch(_POPEN, new_callable=MagicMock) as popen_mock,
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
            task = Task.objects.create(ticket=self.ticket, session=session)

            launch_web_session(task, phase="coding", overlay_skill_metadata={})

            call_args = popen_mock.call_args[0][0]
            assert "--append-system-prompt" in call_args
            assert "--resume" not in call_args
