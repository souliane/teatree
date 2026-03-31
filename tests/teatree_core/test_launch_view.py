import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

if TYPE_CHECKING:
    import pytest

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestLaunchTaskView(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="test")
        cls.session = Session.objects.create(ticket=cls.ticket, overlay="test", agent_id="agent-1")

    @override_settings(
        TEATREE_INTERACTIVE_RUNTIME="claude-code",
        TEATREE_TERMINAL_MODE="same-terminal",
    )
    def test_interactive_task_returns_url(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        with (
            patch("teatree.agents.web_terminal.shutil.which", return_value="/usr/bin/ttyd"),
            patch("teatree.agents.web_terminal._find_free_port", return_value=9999),
            patch("teatree.agents.web_terminal.subprocess.Popen", MagicMock()),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:9999"
        assert TaskAttempt.objects.count() == 1

    @override_settings(
        TEATREE_HEADLESS_RUNTIME="claude-code",
        TASKS={
            "default": {
                "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
            },
        },
    )
    def test_headless_task_enqueues_background_job(self) -> None:
        import subprocess as _sp  # noqa: PLC0415

        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            phase="coding",
        )

        with (
            patch("teatree.agents.headless.shutil.which", return_value="/usr/bin/claude-code"),
            patch(
                "teatree.agents.headless.subprocess.run",
                return_value=_sp.CompletedProcess([], 0, '{"summary": "OK"}', ""),
            ),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["status"] == "queued"

    def test_returns_404_for_missing_task(self) -> None:
        response = Client().post("/tasks/99999/launch/")

        assert response.status_code == 404

    def test_returns_409_when_task_already_claimed(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        task.claim(claimed_by="other-worker")

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 409
        assert data["error"] == "Task already claimed"

    def test_returns_409_when_task_already_finished(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session, status=Task.Status.COMPLETED)

        response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 409
        assert data["error"] == "Task already finished"

    def test_fails_task_when_overlay_lookup_raises(self) -> None:
        def _raise() -> None:
            msg = "overlay unavailable"
            raise RuntimeError(msg)

        task = Task.objects.create(ticket=self.ticket, session=self.session)

        with patch("teatree.core.views.launch.get_overlay", side_effect=_raise):
            response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert data["error"] == "overlay unavailable"

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1

    @override_settings(
        TEATREE_INTERACTIVE_RUNTIME="claude-code",
        TEATREE_TERMINAL_MODE="same-terminal",
    )
    def test_returns_json_error_and_stores_attempt_on_failure(self) -> None:
        def _raise(*_args: object, **_kw: object) -> None:
            msg = "ttyd is not installed"
            raise FileNotFoundError(msg)

        task = Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            phase="coding",
        )

        with (
            patch("teatree.agents.web_terminal.launch_web_session", side_effect=_raise),
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            response = Client().post(f"/tasks/{task.pk}/launch/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert data["error"] == "ttyd is not installed"

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1


class TestLaunchTerminalView:
    def test_spawns_ttyd_with_shell(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        popen_mock = MagicMock()
        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teatree.core.views.launch._find_free_port", lambda: 7777)
        monkeypatch.setattr("teatree.core.views.launch.subprocess.Popen", popen_mock)
        monkeypatch.setenv("SHELL", "/bin/zsh")

        response = Client().post("/dashboard/launch-terminal/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:7777"

        args = popen_mock.call_args[0][0]
        assert args[0] == "/usr/bin/ttyd"
        assert "/bin/zsh" in args
        assert "-l" in args

    def test_returns_500_when_ttyd_missing(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda _name: None)

        response = Client().post("/dashboard/launch-terminal/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "ttyd not installed" in data["error"]


class TestFindFreePort:
    def test_returns_valid_port(self) -> None:
        from teatree.core.views.launch import _find_free_port  # noqa: PLC0415

        port = _find_free_port()
        assert isinstance(port, int)
        assert port > 0


class TestLaunchInteractiveForTask:
    def test_returns_url(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from teatree.core.views.launch import launch_interactive_for_task  # noqa: PLC0415

        popen_mock = MagicMock()
        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teatree.core.views.launch._find_free_port", lambda: 8888)
        monkeypatch.setattr("teatree.core.views.launch.subprocess.Popen", popen_mock)

        mock_task = MagicMock()
        mock_task.pk = 42

        url = launch_interactive_for_task(mock_task)

        assert url == "http://127.0.0.1:8888"
        popen_mock.assert_called_once()

    def test_returns_empty_when_binaries_missing(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        from teatree.core.views.launch import launch_interactive_for_task  # noqa: PLC0415

        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda _name: None)

        mock_task = MagicMock()
        mock_task.pk = 1

        url = launch_interactive_for_task(mock_task)

        assert url == ""


class TestLaunchInteractiveAgentView:
    def test_spawns_ttyd_with_claude(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        popen_mock = MagicMock()
        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr("teatree.core.views.launch._find_free_port", lambda: 6666)
        monkeypatch.setattr("teatree.core.views.launch.subprocess.Popen", popen_mock)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 200
        assert data["launch_url"] == "http://127.0.0.1:6666"

        args = popen_mock.call_args[0][0]
        assert "/usr/bin/ttyd" in args[0]
        assert "/usr/bin/claude" in args

    def test_returns_500_when_claude_missing(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        monkeypatch.setattr("teatree.core.views.launch.shutil.which", lambda _name: None)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "claude CLI not found" in data["error"]

    def test_returns_500_when_ttyd_missing(self, monkeypatch: "pytest.MonkeyPatch") -> None:
        def _selective_which(name: str) -> str | None:
            if name == "claude":
                return "/usr/bin/claude"
            return None

        monkeypatch.setattr("teatree.core.views.launch.shutil.which", _selective_which)

        response = Client().post("/dashboard/launch-agent/")
        data = json.loads(response.content)

        assert response.status_code == 500
        assert "ttyd not installed" in data["error"]
