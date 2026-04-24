"""Tests for teatree.agents.web_terminal — ttyd session launching."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

import teatree.agents.terminal_launcher as terminal_launcher_mod
import teatree.agents.web_terminal as web_terminal_mod
import teatree.utils.run as utils_run_mod
from teatree.agents.web_terminal import (
    get_resume_session_id,
    launch_web_session,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.utils.ports import find_free_port

# --- _find_free_port ---


def test_find_free_port_returns_int() -> None:
    port = find_free_port()
    assert isinstance(port, int)
    assert port > 0


# --- get_resume_session_id ---


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

        assert get_resume_session_id(task) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_non_uuid_agent_id(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert get_resume_session_id(task) == ""

    def test_empty_agent_id(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="")
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert get_resume_session_id(task) == ""


# --- launch_web_session ---


class TestLaunchWebSession(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_creates_attempt(self) -> None:
        with (
            patch.object(web_terminal_mod.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(terminal_launcher_mod, "find_free_port", return_value=8888),
            patch.object(utils_run_mod.subprocess, "Popen", new_callable=MagicMock),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = launch_web_session(task, overlay_skill_metadata={})

            assert attempt.launch_url == "http://127.0.0.1:8888"
            assert TaskAttempt.objects.count() == 1

    def test_raises_when_claude_missing(self) -> None:
        with (
            patch.object(web_terminal_mod.shutil, "which", return_value=None),
            patch.object(terminal_launcher_mod, "find_free_port", return_value=8888),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            with pytest.raises(FileNotFoundError, match="claude CLI is not installed"):
                launch_web_session(task, overlay_skill_metadata={})

    def test_returns_empty_url_when_ttyd_missing(self) -> None:
        def which_mock(name: str) -> str | None:
            return "/usr/bin/claude" if name == "claude" else None

        with (
            patch.object(web_terminal_mod.shutil, "which", side_effect=which_mock),
        ):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = launch_web_session(task, overlay_skill_metadata={})

            assert attempt.launch_url == ""
            assert TaskAttempt.objects.count() == 1

    def test_resumes_session(self) -> None:
        with (
            patch.object(web_terminal_mod.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(terminal_launcher_mod, "find_free_port", return_value=7777),
            patch.object(utils_run_mod.subprocess, "Popen", new_callable=MagicMock) as popen_mock,
        ):
            session = Session.objects.create(
                ticket=self.ticket,
                agent_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = launch_web_session(task, overlay_skill_metadata={})

            assert attempt.launch_url == "http://127.0.0.1:7777"

            # Verify --resume was passed
            call_args = popen_mock.call_args[0][0]
            assert "--resume" in call_args
            resume_idx = call_args.index("--resume")
            assert call_args[resume_idx + 1] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_new_session_uses_system_context(self) -> None:
        with (
            patch.object(web_terminal_mod.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch.object(terminal_launcher_mod, "find_free_port", return_value=7777),
            patch.object(utils_run_mod.subprocess, "Popen", new_callable=MagicMock) as popen_mock,
        ):
            session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
            task = Task.objects.create(ticket=self.ticket, session=session)

            launch_web_session(task, overlay_skill_metadata={})

            call_args = popen_mock.call_args[0][0]
            assert "--append-system-prompt" in call_args
            assert "--resume" not in call_args


def test_detect_available_apps_returns_list() -> None:
    from teatree.agents.terminal_launcher import detect_available_apps  # noqa: PLC0415

    apps = detect_available_apps()
    assert isinstance(apps, list)
    for value, label in apps:
        assert isinstance(value, str)
        assert isinstance(label, str)
