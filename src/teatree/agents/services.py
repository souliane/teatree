from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from teatree.core.models import Task


@dataclass(frozen=True, slots=True)
class RuntimeExecution:
    runtime: str
    artifact_path: str
    metadata: dict[str, object] = field(default_factory=dict)
    reroute_to: str | None = None


class RuntimeAdapter(Protocol):
    def run(  # pragma: no cover
        self,
        *,
        task: "Task",
        skills: list[str],
        terminal_mode: str | None = None,
    ) -> RuntimeExecution: ...


class EchoRuntime:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, *, task: "Task", skills: list[str], terminal_mode: str | None = None) -> RuntimeExecution:
        return RuntimeExecution(
            runtime=self.name,
            artifact_path=str(Path("artifacts") / f"task-{task.pk}-{self.name}.json"),
            metadata={"skills": skills, "terminal_mode": terminal_mode},
        )


_RUNTIME_REGISTRY: dict[str, RuntimeAdapter] = {
    "claude-code": EchoRuntime("claude-code"),
}


def register_runtime(name: str, adapter: RuntimeAdapter) -> None:
    _RUNTIME_REGISTRY[name] = adapter


def reset_runtime_registry() -> None:
    _RUNTIME_REGISTRY.clear()
    _RUNTIME_REGISTRY["claude-code"] = EchoRuntime("claude-code")


def get_runtime(name: str = "claude-code") -> RuntimeAdapter:
    try:
        return _RUNTIME_REGISTRY[name]
    except KeyError as exc:
        msg = f"Unknown TeaTree runtime: {name}"
        raise ImproperlyConfigured(msg) from exc


def get_headless_runtime_name() -> str:
    return "claude-code"


def get_interactive_runtime_name() -> str:
    return "claude-code"


def get_terminal_mode() -> str:
    return getattr(settings, "TEATREE_TERMINAL_MODE", "ttyd")
