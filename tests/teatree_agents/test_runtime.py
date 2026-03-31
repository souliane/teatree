from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import TestCase, override_settings

from teatree.agents.sdk import run_headless_task
from teatree.agents.services import (
    RuntimeExecution,
    get_headless_runtime_name,
    get_interactive_runtime_name,
    get_runtime,
    get_terminal_mode,
    register_runtime,
    reset_runtime_registry,
)
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.agents.terminal import run_interactive_task
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class RecordingRuntime:
    def __init__(self, name: str, *, reroute_to: str | None = None) -> None:
        self.name = name
        self.reroute_to = reroute_to
        self.calls: list[tuple[int, list[str]]] = []

    def run(self, *, task: Task, skills: list[str], terminal_mode: str | None = None) -> RuntimeExecution:
        self.calls.append((int(task.pk), skills))
        return RuntimeExecution(
            runtime=self.name,
            artifact_path=f"artifacts/task-{task.pk}-{self.name}.json",
            metadata={"terminal_mode": terminal_mode},
            reroute_to=self.reroute_to,
        )


@pytest.fixture(autouse=True)
def reset_runtimes() -> Iterator[None]:
    reset_runtime_registry()
    yield
    reset_runtime_registry()


def test_resolve_skill_bundle_with_overlay_and_phase() -> None:
    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={"skill_path": "/skills/acme/SKILL.md"},
    )
    assert "/skills/acme/SKILL.md" in bundle
    assert "code" in bundle


def test_resolve_skill_bundle_ignores_unknown_phase() -> None:
    bundle = resolve_skill_bundle(
        phase="unknown-phase",
        overlay_skill_metadata={"skill_path": "code"},
    )
    assert "code" in bundle


def test_resolve_skill_bundle_uses_framework_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tmp'\n", encoding="utf-8")

    bundle = resolve_skill_bundle(phase="debugging", overlay_skill_metadata={})

    assert "ac-python" in bundle
    assert "debug" in bundle


class TestRunHeadlessTask(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket, agent_id="agent-1")

    @override_settings(TEATREE_HEADLESS_RUNTIME="test-sdk")
    def test_records_attempt_and_completes_work(self) -> None:
        runtime = RecordingRuntime("test-sdk")
        register_runtime("test-sdk", runtime)
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        result = run_headless_task(
            task,
            phase="coding",
            overlay_skill_metadata={"skill_path": "/skills/acme/SKILL.md"},
        )

        task.refresh_from_db()

        assert result.artifact_path == f"artifacts/task-{task.pk}-test-sdk.json"
        assert "/skills/acme/SKILL.md" in runtime.calls[0][1]
        assert "code" in runtime.calls[0][1]
        assert task.status == Task.Status.COMPLETED
        assert TaskAttempt.objects.count() == 1

    @override_settings(TEATREE_HEADLESS_RUNTIME="failing")
    def test_records_failure_attempt_on_runtime_error(self) -> None:
        register_runtime("failing", FailingRuntime())
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        with pytest.raises(RuntimeError, match="runtime crashed"):
            run_headless_task(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()

        assert task.status == Task.Status.FAILED
        attempt = TaskAttempt.objects.get(task=task)
        assert attempt.exit_code == 1
        assert attempt.error == "runtime crashed"


class TestRunInteractiveTask(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket, agent_id="agent-1")

    @override_settings(TEATREE_INTERACTIVE_RUNTIME="test-terminal", TEATREE_TERMINAL_MODE="same-terminal")
    def test_can_reroute_to_interactive(self) -> None:
        runtime = RecordingRuntime("test-terminal", reroute_to=Task.ExecutionTarget.INTERACTIVE)
        register_runtime("test-terminal", runtime)
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        result = run_interactive_task(
            task,
            phase="debugging",
            overlay_skill_metadata={},
        )

        task.refresh_from_db()

        assert result.metadata == {"terminal_mode": "same-terminal"}
        assert task.status == Task.Status.PENDING
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert TaskAttempt.objects.count() == 1

    @override_settings(
        TEATREE_HEADLESS_RUNTIME="claude-code",
        TEATREE_INTERACTIVE_RUNTIME="codex",
        TEATREE_TERMINAL_MODE="new-window",
    )
    def test_expose_defaults_and_complete_interactive_work(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        result = run_interactive_task(
            task,
            phase="reviewing",
            overlay_skill_metadata={},
        )

        task.refresh_from_db()

        assert get_headless_runtime_name() == "claude-code"
        assert get_interactive_runtime_name() == "codex"
        assert get_terminal_mode() == "new-window"
        assert result.artifact_path == f"artifacts/task-{task.pk}-codex.json"
        assert task.status == Task.Status.COMPLETED
        assert get_runtime("claude-code").run(task=task, skills=[]).runtime == "claude-code"

    @override_settings(TEATREE_INTERACTIVE_RUNTIME="failing", TEATREE_TERMINAL_MODE="same-terminal")
    def test_records_failure_attempt_on_runtime_error(self) -> None:
        register_runtime("failing", FailingRuntime())
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        with pytest.raises(RuntimeError, match="runtime crashed"):
            run_interactive_task(task, phase="debugging", overlay_skill_metadata={})

        task.refresh_from_db()

        assert task.status == Task.Status.FAILED
        assert TaskAttempt.objects.filter(task=task).count() == 1


def test_get_runtime_raises_for_unknown_runtime() -> None:
    with pytest.raises(Exception, match="Unknown TeaTree runtime: missing-runtime"):
        get_runtime("missing-runtime")


class FailingRuntime:
    def run(self, *, task: Task, skills: list[str], terminal_mode: str | None = None) -> RuntimeExecution:
        msg = "runtime crashed"
        raise RuntimeError(msg)
