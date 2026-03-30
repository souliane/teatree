import json
from unittest.mock import MagicMock

import pytest
from django.test import Client, override_settings

from teetree.core.models import Session, Task, TaskAttempt, Ticket

_OVERLAY = "tests.teetree_core.conftest.CommandOverlay"


@pytest.mark.django_db
class TestLaunchTaskView:
    @override_settings(
        TEATREE_OVERLAY_CLASS=_OVERLAY,
        TEATREE_INTERACTIVE_RUNTIME="claude-code",
        TEATREE_TERMINAL_MODE="same-terminal",
    )
    def test_interactive_task_returns_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teetree.agents.web_terminal.shutil.which", lambda _name: "/usr/bin/ttyd")
        monkeypatch.setattr("teetree.agents.web_terminal._find_free_port", lambda: 9999)
        monkeypatch.setattr("teetree.agents.web_terminal.subprocess.Popen", MagicMock())

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:9999"
        assert TaskAttempt.objects.count() == 1

    @override_settings(
        TEATREE_OVERLAY_CLASS=_OVERLAY,
        TEATREE_HEADLESS_RUNTIME="claude-code",
        TASKS={
            "default": {
                "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
            },
        },
    )
    def test_headless_task_enqueues_background_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teetree.agents.headless.shutil.which", lambda _name: "/usr/bin/claude-code")
        monkeypatch.setattr(
            "teetree.agents.headless.subprocess.run",
            lambda *_args, **_kwargs: __import__("subprocess").CompletedProcess([], 0, '{"summary": "OK"}', ""),
        )

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="coding",
        )

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["status"] == "queued"

    def test_returns_404_for_missing_task(self) -> None:
        response = Client().post("/tasks/99999/launch/")

        assert response.status_code == 404

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_returns_409_when_task_already_claimed(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)
        task.claim(claimed_by="other-worker")

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 409
        assert data["error"] == "Task already claimed"

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_returns_409_when_task_already_finished(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.COMPLETED)

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 409
        assert data["error"] == "Task already finished"

    @override_settings(TEATREE_OVERLAY_CLASS=_OVERLAY)
    def test_fails_task_when_overlay_lookup_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> None:
            msg = "overlay unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr("teetree.core.views.launch.get_overlay", _raise)

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert data["error"] == "overlay unavailable"

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1

    @override_settings(
        TEATREE_OVERLAY_CLASS=_OVERLAY,
        TEATREE_INTERACTIVE_RUNTIME="claude-code",
        TEATREE_TERMINAL_MODE="same-terminal",
    )
    def test_returns_json_error_and_stores_attempt_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kw: object) -> None:
            msg = "ttyd is not installed"
            raise FileNotFoundError(msg)

        monkeypatch.setattr("teetree.agents.web_terminal.launch_web_session", _raise)

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert data["error"] == "ttyd is not installed"

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1


class TestLaunchTerminalView:
    def test_spawns_ttyd_with_shell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        popen_mock = MagicMock()
        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teetree.core.views.launch._find_free_port", lambda: 7777)
        monkeypatch.setattr("teetree.core.views.launch.subprocess.Popen", popen_mock)
        monkeypatch.setenv("SHELL", "/bin/zsh")

        response = Client().post("/dashboard/launch-terminal/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:7777"

        args = popen_mock.call_args[0][0]
        assert args[0] == "/usr/bin/ttyd"
        assert "/bin/zsh" in args
        assert "-l" in args

    def test_returns_500_when_ttyd_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda _name: None)

        response = Client().post("/dashboard/launch-terminal/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "ttyd not installed" in data["error"]


class TestFindFreePort:
    def test_returns_valid_port(self) -> None:
        from teetree.core.views.launch import _find_free_port  # noqa: PLC0415

        port = _find_free_port()
        assert isinstance(port, int)
        assert port > 0


class TestLaunchInteractiveForTask:
    def test_returns_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teetree.core.views.launch import launch_interactive_for_task  # noqa: PLC0415

        popen_mock = MagicMock()
        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teetree.core.views.launch._find_free_port", lambda: 8888)
        monkeypatch.setattr("teetree.core.views.launch.subprocess.Popen", popen_mock)

        mock_task = MagicMock()
        mock_task.pk = 42

        url = launch_interactive_for_task(mock_task)

        assert url == "http://127.0.0.1:8888"
        popen_mock.assert_called_once()

    def test_returns_empty_when_binaries_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from teetree.core.views.launch import launch_interactive_for_task  # noqa: PLC0415

        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda _name: None)

        mock_task = MagicMock()
        mock_task.pk = 1

        url = launch_interactive_for_task(mock_task)

        assert url == ""


class TestLaunchInteractiveAgentView:
    def test_spawns_ttyd_with_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        popen_mock = MagicMock()
        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teetree.core.views.launch._find_free_port", lambda: 6666)
        monkeypatch.setattr("teetree.core.views.launch.subprocess.Popen", popen_mock)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:6666"

        args = popen_mock.call_args[0][0]
        assert "/usr/bin/ttyd" in args[0]
        assert "/usr/bin/claude" in args

    def test_returns_500_when_claude_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teetree.core.views.launch.shutil.which", lambda _name: None)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "claude CLI not found" in data["error"]

    def test_returns_500_when_ttyd_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _selective_which(name: str) -> str | None:
            if name == "claude":
                return "/usr/bin/claude"
            return None

        monkeypatch.setattr("teetree.core.views.launch.shutil.which", _selective_which)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "ttyd not installed" in data["error"]
