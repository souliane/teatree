"""Tests for teetree.agents.web_terminal — ttyd session launching."""

from unittest.mock import MagicMock

import pytest

from teetree.agents.web_terminal import (
    _find_free_port,
    _get_resume_session_id,
    launch_web_session,
)
from teetree.core.models import Session, Task, TaskAttempt, Ticket

# --- _find_free_port ---


def test_find_free_port_returns_int() -> None:
    port = _find_free_port()
    assert isinstance(port, int)
    assert port > 0


# --- _get_resume_session_id ---


@pytest.mark.django_db
def test_get_resume_session_id_with_uuid_agent_id() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(
        ticket=ticket,
        agent_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    )
    task = Task.objects.create(ticket=ticket, session=session)

    assert _get_resume_session_id(task) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.mark.django_db
def test_get_resume_session_id_non_uuid_agent_id() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id="not-a-uuid")
    task = Task.objects.create(ticket=ticket, session=session)

    assert _get_resume_session_id(task) == ""


@pytest.mark.django_db
def test_get_resume_session_id_empty_agent_id() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id="")
    task = Task.objects.create(ticket=ticket, session=session)

    assert _get_resume_session_id(task) == ""


# --- launch_web_session ---


@pytest.mark.django_db
def test_launch_web_session_creates_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 8888)
    monkeypatch.setattr("teetree.agents.web_terminal.subprocess.Popen", MagicMock())

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    attempt = launch_web_session(task, phase="coding", overlay_skill_metadata={})

    assert attempt.launch_url == "http://127.0.0.1:8888"
    assert TaskAttempt.objects.count() == 1


@pytest.mark.django_db
def test_launch_web_session_raises_when_claude_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", lambda name: None)
    monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 8888)

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    with pytest.raises(FileNotFoundError, match="claude CLI is not installed"):
        launch_web_session(task, phase="coding", overlay_skill_metadata={})


@pytest.mark.django_db
def test_launch_web_session_raises_when_ttyd_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def which_mock(name: str) -> str | None:
        return "/usr/bin/claude" if name == "claude" else None

    monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", which_mock)
    monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 8888)

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    with pytest.raises(FileNotFoundError, match="ttyd is not installed"):
        launch_web_session(task, phase="coding", overlay_skill_metadata={})


@pytest.mark.django_db
def test_launch_web_session_resumes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    popen_mock = MagicMock()
    monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 7777)
    monkeypatch.setattr("teetree.agents.web_terminal.subprocess.Popen", popen_mock)

    ticket = Ticket.objects.create()
    session = Session.objects.create(
        ticket=ticket,
        agent_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    )
    task = Task.objects.create(ticket=ticket, session=session)

    attempt = launch_web_session(task, phase="coding", overlay_skill_metadata={})

    assert attempt.launch_url == "http://127.0.0.1:7777"

    # Verify --resume was passed
    call_args = popen_mock.call_args[0][0]
    assert "--resume" in call_args
    resume_idx = call_args.index("--resume")
    assert call_args[resume_idx + 1] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.mark.django_db
def test_launch_web_session_new_session_uses_system_context(monkeypatch: pytest.MonkeyPatch) -> None:
    popen_mock = MagicMock()
    monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 7777)
    monkeypatch.setattr("teetree.agents.web_terminal.subprocess.Popen", popen_mock)

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id="not-a-uuid")
    task = Task.objects.create(ticket=ticket, session=session)

    launch_web_session(task, phase="coding", overlay_skill_metadata={})

    call_args = popen_mock.call_args[0][0]
    assert "--append-system-prompt" in call_args
    assert "--resume" not in call_args
