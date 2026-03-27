from collections.abc import Iterator
from pathlib import Path

import pytest
from django.test import override_settings

from teetree import skill_loading as skill_loading_module
from teetree.agents.sdk import run_headless_task
from teetree.agents.services import (
    RuntimeExecution,
    get_headless_runtime_name,
    get_interactive_runtime_name,
    get_runtime,
    get_terminal_mode,
    register_runtime,
    reset_runtime_registry,
)
from teetree.agents.skill_bundle import resolve_skill_bundle
from teetree.agents.terminal import run_interactive_task
from teetree.core.models import Session, Task, TaskAttempt, Ticket


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


@pytest.fixture(autouse=True)
def install_framework_skill_fixtures(tmp_path: Path) -> Iterator[None]:
    for name, body in {
        "ac-python": "---\nname: ac-python\n---\n",
        "ac-django": "---\nname: ac-django\nrequires:\n  - ac-python\n---\n",
    }.items():
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    original = list(skill_loading_module.DEFAULT_SKILL_SEARCH_DIRS)
    skill_loading_module.DEFAULT_SKILL_SEARCH_DIRS[:] = [tmp_path, *original]
    try:
        yield
    finally:
        skill_loading_module.DEFAULT_SKILL_SEARCH_DIRS[:] = original


def test_resolve_skill_bundle_merges_overlay_and_phase_skills() -> None:
    bundle = resolve_skill_bundle(
        phase="coding",
        overlay_skill_metadata={
            "skill_path": "/skills/acme/SKILL.md",
        },
        delegation_map_path=Path("references/skill-delegation.md"),
    )

    assert bundle == [
        "/skills/acme/SKILL.md",
        "ac-python",
        "ac-django",
        "t3-rules",
        "t3-workspace",
        "t3-code",
    ]


def test_resolve_skill_bundle_ignores_unknown_phase() -> None:
    bad_metadata: dict[str, object] = {
        "skill_path": "t3-code",
    }
    bundle = resolve_skill_bundle(
        phase="unknown-phase",
        overlay_skill_metadata=bad_metadata,
        delegation_map_path=Path("references/skill-delegation.md"),
    )

    assert bundle == ["t3-rules", "t3-workspace", "t3-code", "ac-python", "ac-django"]


def test_resolve_skill_bundle_uses_builtin_default_when_local_map_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tmp'\n", encoding="utf-8")

    bundle = resolve_skill_bundle(phase="debugging", overlay_skill_metadata={})

    assert bundle == ["ac-python", "t3-rules", "t3-workspace", "t3-debug"]


@override_settings(TEATREE_HEADLESS_RUNTIME="test-sdk")
@pytest.mark.django_db
def test_run_headless_task_records_attempt_and_completes_work() -> None:
    runtime = RecordingRuntime("test-sdk")
    register_runtime("test-sdk", runtime)
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id="agent-1")
    task = Task.objects.create(ticket=ticket, session=session)

    result = run_headless_task(
        task,
        phase="coding",
        overlay_skill_metadata={"skill_path": "/skills/acme/SKILL.md"},
        delegation_map_path=Path("references/skill-delegation.md"),
    )

    task.refresh_from_db()

    assert result.artifact_path == f"artifacts/task-{task.pk}-test-sdk.json"
    assert runtime.calls[0][1] == [
        "/skills/acme/SKILL.md",
        "ac-python",
        "ac-django",
        "t3-rules",
        "t3-workspace",
        "t3-code",
    ]
    assert task.status == Task.Status.COMPLETED
    assert TaskAttempt.objects.count() == 1


@override_settings(TEATREE_INTERACTIVE_RUNTIME="test-terminal", TEATREE_TERMINAL_MODE="same-terminal")
@pytest.mark.django_db
def test_run_interactive_task_can_reroute_to_interactive() -> None:
    runtime = RecordingRuntime("test-terminal", reroute_to=Task.ExecutionTarget.INTERACTIVE)
    register_runtime("test-terminal", runtime)
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id="agent-1")
    task = Task.objects.create(ticket=ticket, session=session)

    result = run_interactive_task(
        task,
        phase="debugging",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
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
@pytest.mark.django_db
def test_runtime_services_expose_defaults_and_complete_interactive_work() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    result = run_interactive_task(
        task,
        phase="reviewing",
        overlay_skill_metadata={},
        delegation_map_path=Path("references/skill-delegation.md"),
    )

    task.refresh_from_db()

    assert get_headless_runtime_name() == "claude-code"
    assert get_interactive_runtime_name() == "codex"
    assert get_terminal_mode() == "new-window"
    assert result.artifact_path == f"artifacts/task-{task.pk}-codex.json"
    assert task.status == Task.Status.COMPLETED
    assert get_runtime("claude-code").run(task=task, skills=[]).runtime == "claude-code"


def test_get_runtime_raises_for_unknown_runtime() -> None:
    with pytest.raises(Exception, match="Unknown TeaTree runtime: missing-runtime"):
        get_runtime("missing-runtime")


class FailingRuntime:
    def run(self, *, task: Task, skills: list[str], terminal_mode: str | None = None) -> RuntimeExecution:
        msg = "runtime crashed"
        raise RuntimeError(msg)


@override_settings(TEATREE_HEADLESS_RUNTIME="failing")
@pytest.mark.django_db
def test_run_headless_task_records_failure_attempt_on_runtime_error() -> None:
    register_runtime("failing", FailingRuntime())
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        run_headless_task(task, phase="coding", overlay_skill_metadata={})

    task.refresh_from_db()

    assert task.status == Task.Status.FAILED
    attempt = TaskAttempt.objects.get(task=task)
    assert attempt.exit_code == 1
    assert attempt.error == "runtime crashed"


@override_settings(TEATREE_INTERACTIVE_RUNTIME="failing", TEATREE_TERMINAL_MODE="same-terminal")
@pytest.mark.django_db
def test_run_interactive_task_records_failure_attempt_on_runtime_error() -> None:
    register_runtime("failing", FailingRuntime())
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    with pytest.raises(RuntimeError, match="runtime crashed"):
        run_interactive_task(task, phase="debugging", overlay_skill_metadata={})

    task.refresh_from_db()

    assert task.status == Task.Status.FAILED
    assert TaskAttempt.objects.filter(task=task).count() == 1
