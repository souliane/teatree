"""Shared fixtures for teatree script tests."""

import importlib.util
import os
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Guard against import-time side effects in script modules that call _init.init()
# at module import. Route HOME/T3_WORKSPACE_DIR to a disposable temp sandbox.
_IMPORT_SANDBOX = tempfile.TemporaryDirectory(prefix="teatree-tests-import-")
_IMPORT_HOME = Path(_IMPORT_SANDBOX.name) / "home"
_IMPORT_WORKSPACE = _IMPORT_HOME / "workspace"
_IMPORT_WORKSPACE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HOME", str(_IMPORT_HOME))
os.environ.setdefault("T3_WORKSPACE_DIR", str(_IMPORT_WORKSPACE))


def load_script(name: str) -> types.ModuleType:
    """Dynamically load a teatree script as a module for testing."""
    p = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_mod", p)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """Create a MagicMock simulating a successful subprocess.run result."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture
def pg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set standard Postgres env vars for testing."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace structure with a main repo."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    repo = ws / "my-project"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "manage.py").touch()

    return ws


@pytest.fixture
def ticket_dir(workspace: Path) -> Path:
    """Create a ticket directory with a worktree inside the workspace."""
    td = workspace / "my-project-1234-test-fix"
    td.mkdir()

    wt = td / "my-project"
    wt.mkdir()
    # In worktrees, .git is a file (not a directory)
    (wt / ".git").write_text("gitdir: /some/path/.git/worktrees/my-project")

    return td


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Clear the extension point registry between tests."""
    from lib.registry import clear  # noqa: PLC0415

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _no_system_port_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent lsof calls from find_free_ports during tests."""
    monkeypatch.setattr("lib.env.port_in_use", lambda _port: False)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate process env so tests cannot touch host workspace/config."""
    home = tmp_path / "home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    monkeypatch.delenv("T3_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("T3_BRANCH_PREFIX", raising=False)
    monkeypatch.delenv("TICKET_DIR", raising=False)
    monkeypatch.delenv("WT_VARIANT", raising=False)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
