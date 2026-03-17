# Worktree State Machine & `t3` CLI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a django-fsm-style state machine to track worktree lifecycle (`created → provisioned → services_up → ready`) and expose it via a `t3` CLI that both users and the LLM call.

**Architecture:** A `@transition` decorator (~40 lines) gates lifecycle methods with guards and side effects. The `WorktreeLifecycle` class wraps existing Python functions (`wt_setup.py`, `find_free_ports`, `registry.call`). State persists to `.state.json` per ticket dir. A `t3` typer CLI replaces shell function wrappers in `bootstrap.sh`. Mermaid diagrams auto-generate from `_fsm` metadata on decorated methods.

**Tech Stack:** Python 3.12+, typer, existing teatree lib (registry, env, db, ports, extension_points)

**Repo:** `~/workspace/souliane/teatree`

**Existing code to build on:**

- `scripts/lib/registry.py` — 3-layer extension point registry (46 lines)
- `scripts/lib/env.py` — `WorktreeContext`, `find_free_ports`, `resolve_context`
- `scripts/lib/db.py` — `db_exists`, `db_restore`, `worktree_db_name`
- `scripts/lib/ports.py` — `port_in_use`, `free_port`
- `scripts/lib/extension_points.py` — default no-op implementations, `register_defaults()`
- `scripts/lib/bootstrap.sh` — shell wrappers (`t3_ticket`, `t3_setup`, etc.)
- `scripts/wt_setup.py` — full setup orchestration (phases 1-2b)
- `tests/conftest.py` — fixtures: `workspace`, `ticket_dir`, `pg_env`, registry auto-clear

**Testing:** 100% coverage required (`pyproject.toml: fail_under = 100`). Tests use `tmp_path`, `monkeypatch`, mock subprocess. No real DB/Docker calls.

---

## Task 1: `@transition` Decorator

**Files:**

- Create: `scripts/lib/fsm.py`
- Test: `tests/test_fsm.py`

### Step 1: Write the failing test

```python
# tests/test_fsm.py
"""Tests for the finite state machine decorator."""

import pytest

from lib.fsm import ConditionFailed, InvalidTransition, transition


def _always_true(self) -> bool:
    return True


def _always_false(self) -> bool:
    return False


class FakeLifecycle:
    state: str = "idle"

    def save(self) -> None:
        """No-op persistence for testing."""

    @transition(source="idle", target="active", conditions=[_always_true])
    def activate(self) -> str:
        return "activated"

    @transition(source="active", target="idle")
    def deactivate(self) -> None:
        pass

    @transition(source="idle", target="blocked", conditions=[_always_false])
    def block(self) -> None:
        pass

    @transition(source=["active", "idle"], target="done")
    def finish(self) -> None:
        pass

    @transition(source="*", target="idle")
    def reset(self) -> None:
        pass


class TestTransitionDecorator:
    def test_valid_transition(self) -> None:
        obj = FakeLifecycle()
        result = obj.activate()
        assert result == "activated"
        assert obj.state == "active"

    def test_invalid_source_state(self) -> None:
        obj = FakeLifecycle()
        with pytest.raises(InvalidTransition, match="Cannot deactivate from idle"):
            obj.deactivate()

    def test_condition_blocks_transition(self) -> None:
        obj = FakeLifecycle()
        with pytest.raises(ConditionFailed, match="_always_false"):
            obj.block()
        assert obj.state == "idle"  # state unchanged

    def test_multi_source_states(self) -> None:
        obj = FakeLifecycle()
        obj.finish()
        assert obj.state == "done"

    def test_wildcard_source(self) -> None:
        obj = FakeLifecycle()
        obj.state = "anything"
        obj.reset()
        assert obj.state == "idle"

    def test_fsm_metadata_on_method(self) -> None:
        assert hasattr(FakeLifecycle.activate, "_fsm")
        meta = FakeLifecycle.activate._fsm
        assert meta["source"] == ["idle"]
        assert meta["target"] == "active"
        assert len(meta["conditions"]) == 1


class TestIntrospection:
    def test_get_all_transitions(self) -> None:
        from lib.fsm import get_transitions

        transitions = get_transitions(FakeLifecycle)
        names = [t["method"] for t in transitions]
        assert "activate" in names
        assert "deactivate" in names
        assert "reset" in names

    def test_get_available_transitions(self) -> None:
        from lib.fsm import get_available_transitions

        obj = FakeLifecycle()
        available = get_available_transitions(obj)
        names = [t["method"] for t in available]
        assert "activate" in names
        assert "finish" in names
        assert "deactivate" not in names  # source is "active", not "idle"

    def test_generate_mermaid(self) -> None:
        from lib.fsm import generate_mermaid

        mermaid = generate_mermaid(FakeLifecycle)
        assert "stateDiagram-v2" in mermaid
        assert "idle --> active" in mermaid
        assert "activate" in mermaid
```

### Step 2: Run test to verify it fails

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_fsm.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.fsm'`

### Step 3: Write minimal implementation

```python
# scripts/lib/fsm.py
"""Django-fsm-style state machine decorator with introspection."""

import functools
import inspect
from collections.abc import Callable
from typing import Any


class InvalidTransition(Exception):
    pass


class ConditionFailed(Exception):
    pass


def transition(
    source: str | list[str],
    target: str,
    conditions: list[Callable] | None = None,
) -> Callable:
    sources = [source] if isinstance(source, str) else source

    def decorator(method: Callable) -> Callable:
        method._fsm = {  # noqa: SLF001
            "source": sources,
            "target": target,
            "conditions": conditions or [],
        }

        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if "*" not in sources and self.state not in sources:
                msg = f"Cannot {method.__name__} from {self.state}"
                raise InvalidTransition(msg)
            for cond in method._fsm["conditions"]:  # noqa: SLF001
                if not cond(self):
                    msg = f"Condition {cond.__name__} failed for {method.__name__}"
                    raise ConditionFailed(msg)
            result = method(self, *args, **kwargs)
            self.state = target
            if hasattr(self, "save"):
                self.save()
            return result

        return wrapper

    return decorator


def get_transitions(cls: type) -> list[dict[str, Any]]:
    result = []
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        fsm = getattr(method, "_fsm", None)
        if fsm:
            result.append({"method": name, **fsm})
    return result


def get_available_transitions(obj: object) -> list[dict[str, Any]]:
    result = []
    for t in get_transitions(type(obj)):
        if "*" in t["source"] or obj.state in t["source"]:
            result.append(t)
    return result


def generate_mermaid(cls: type) -> str:
    lines = ["stateDiagram-v2"]
    for t in get_transitions(cls):
        label = t["method"]
        cond_names = [c.__name__ for c in t["conditions"]]
        if cond_names:
            label += f" [{', '.join(cond_names)}]"
        sources = t["source"]
        if "*" in sources:
            # Collect all states mentioned as sources or targets
            all_states: set[str] = set()
            for other in get_transitions(cls):
                all_states.update(other["source"])
                all_states.add(other["target"])
            all_states.discard("*")
            sources = sorted(all_states)
        for src in sources:
            lines.append(f"    {src} --> {t['target']} : {label}")
    return "\n".join(lines)
```

### Step 4: Run test to verify it passes

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_fsm.py -x -v`
Expected: PASS — all 9 tests green

### Step 5: Commit

```bash
cd ~/workspace/souliane/teatree
git add scripts/lib/fsm.py tests/test_fsm.py
git commit -m "feat: add django-fsm-style @transition decorator with introspection"
```

---

## Task 2: `WorktreeLifecycle` Class

**Files:**

- Create: `scripts/lib/lifecycle.py`
- Test: `tests/test_lifecycle.py`

### Step 1: Write the failing test

```python
# tests/test_lifecycle.py
"""Tests for worktree lifecycle state machine."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.fsm import ConditionFailed, InvalidTransition


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    td = tmp_path / "ticket-1234"
    td.mkdir()
    return td


@pytest.fixture
def lifecycle(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> "WorktreeLifecycle":
    from lib.lifecycle import WorktreeLifecycle

    monkeypatch.setenv("T3_WORKSPACE_DIR", str(state_dir.parent))
    return WorktreeLifecycle(ticket_dir=str(state_dir))


class TestStateFile:
    def test_initial_state_is_created(self, lifecycle: "WorktreeLifecycle") -> None:
        assert lifecycle.state == "created"

    def test_save_creates_state_file(self, lifecycle: "WorktreeLifecycle", state_dir: Path) -> None:
        lifecycle.save()
        state_file = state_dir / ".state.json"
        assert state_file.is_file()
        data = json.loads(state_file.read_text())
        assert data["state"] == "created"

    def test_load_restores_state(self, state_dir: Path) -> None:
        from lib.lifecycle import WorktreeLifecycle

        state_file = state_dir / ".state.json"
        state_file.write_text(json.dumps({"state": "provisioned", "ports": {"backend": 8001}}))
        lc = WorktreeLifecycle(ticket_dir=str(state_dir))
        assert lc.state == "provisioned"
        assert lc.facts["ports"]["backend"] == 8001


class TestTransitions:
    @patch("lib.lifecycle.registry")
    @patch("lib.lifecycle.find_free_ports", return_value=(8001, 4201, 5433, 6379))
    @patch("lib.lifecycle.db_exists", return_value=False)
    def test_provision_from_created(
        self,
        mock_db_exists: MagicMock,
        mock_ports: MagicMock,
        mock_registry: MagicMock,
        lifecycle: "WorktreeLifecycle",
        state_dir: Path,
    ) -> None:
        # Create minimal worktree context
        ws = state_dir.parent
        main = ws / "my-project"
        main.mkdir(exist_ok=True)
        (main / ".git").mkdir(exist_ok=True)
        wt = state_dir / "my-project"
        wt.mkdir(exist_ok=True)

        mock_registry.call.return_value = True
        lifecycle.provision(wt_dir=str(wt), main_repo=str(main), variant="")
        assert lifecycle.state == "provisioned"
        assert lifecycle.facts["ports"]["backend"] == 8001

    def test_cannot_start_services_from_created(self, lifecycle: "WorktreeLifecycle") -> None:
        with pytest.raises(InvalidTransition, match="Cannot start_services from created"):
            lifecycle.start_services()

    def test_teardown_from_any_state(self, lifecycle: "WorktreeLifecycle") -> None:
        lifecycle.teardown()
        assert lifecycle.state == "created"


class TestStatus:
    def test_status_returns_dict(self, lifecycle: "WorktreeLifecycle") -> None:
        status = lifecycle.status()
        assert status["state"] == "created"
        assert "available_transitions" in status
        assert "facts" in status

    def test_available_transitions_from_created(self, lifecycle: "WorktreeLifecycle") -> None:
        status = lifecycle.status()
        names = [t["method"] for t in status["available_transitions"]]
        assert "provision" in names
        assert "start_services" not in names
```

### Step 2: Run test to verify it fails

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_lifecycle.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.lifecycle'`

### Step 3: Write minimal implementation

```python
# scripts/lib/lifecycle.py
"""Worktree lifecycle state machine.

States: created → provisioned → services_up → ready
Persists to .state.json in the ticket directory.
"""

import json
from pathlib import Path
from typing import Any

from lib import registry
from lib.db import db_exists
from lib.env import find_free_ports
from lib.fsm import get_available_transitions, transition


class WorktreeLifecycle:
    state: str = "created"

    def __init__(self, ticket_dir: str) -> None:
        self.ticket_dir = ticket_dir
        self.facts: dict[str, Any] = {}
        self._load()

    def _state_file(self) -> Path:
        return Path(self.ticket_dir) / ".state.json"

    def _load(self) -> None:
        sf = self._state_file()
        if sf.is_file():
            data = json.loads(sf.read_text(encoding="utf-8"))
            self.state = data.get("state", "created")
            self.facts = data.get("facts", {})

    def save(self) -> None:
        data = {"state": self.state, "facts": self.facts}
        self._state_file().write_text(
            json.dumps(data, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ticket_dir": self.ticket_dir,
            "facts": self.facts,
            "available_transitions": [
                {"method": t["method"], "conditions": [c.__name__ for c in t["conditions"]]}
                for t in get_available_transitions(self)
            ],
        }

    # --- Guards ---

    def _ports_available(self) -> bool:
        return True  # find_free_ports handles conflicts

    def _db_provisioned(self) -> bool:
        db_name = self.facts.get("db_name", "")
        return bool(db_name) and db_exists(db_name)

    # --- Transitions ---

    @transition(source="created", target="provisioned")
    def provision(self, wt_dir: str, main_repo: str, variant: str) -> None:
        be, fe, pg, rd = find_free_ports(self.ticket_dir)
        self.facts["ports"] = {
            "backend": be,
            "frontend": fe,
            "postgres": pg,
            "redis": rd,
        }
        self.facts["wt_dir"] = wt_dir
        self.facts["main_repo"] = main_repo
        self.facts["variant"] = variant

        # Delegate to extension points (same as wt_setup.py)
        registry.call("wt_symlinks", wt_dir, main_repo, variant)
        registry.call("wt_services", main_repo, wt_dir)

        from lib.db import worktree_db_name
        from lib.env import WorktreeContext

        # Extract ticket number from dir name
        import re

        match = re.search(r"\d+", Path(self.ticket_dir).name)
        ticket_number = match.group() if match else "0"
        db_name = worktree_db_name(ticket_number, variant)
        self.facts["db_name"] = db_name

        if not db_exists(db_name):
            registry.call("wt_db_import", db_name, variant, main_repo)
        registry.call("wt_post_db", wt_dir)

    @transition(source="provisioned", target="services_up")
    def start_services(self) -> None:
        wt_dir = self.facts.get("wt_dir", "")
        registry.call("wt_run_backend", wt_dir)
        registry.call("wt_run_frontend", wt_dir)

    @transition(source="services_up", target="ready")
    def verify(self) -> None:
        self.facts["urls"] = {
            "backend": f"http://localhost:{self.facts['ports']['backend']}",
            "frontend": f"http://localhost:{self.facts['ports']['frontend']}",
        }

    @transition(source=["provisioned", "services_up", "ready"], target="provisioned")
    def db_refresh(self) -> None:
        db_name = self.facts.get("db_name", "")
        variant = self.facts.get("variant", "")
        main_repo = self.facts.get("main_repo", "")
        wt_dir = self.facts.get("wt_dir", "")
        if db_name and main_repo:
            registry.call("wt_db_import", db_name, variant, main_repo)
            registry.call("wt_post_db", wt_dir)

    @transition(source="*", target="created")
    def teardown(self) -> None:
        self.facts = {}
        sf = self._state_file()
        if sf.is_file():
            sf.unlink()
```

### Step 4: Run test to verify it passes

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_lifecycle.py -x -v`
Expected: PASS

### Step 5: Commit

```bash
cd ~/workspace/souliane/teatree
git add scripts/lib/lifecycle.py tests/test_lifecycle.py
git commit -m "feat: add WorktreeLifecycle state machine with .state.json persistence"
```

---

## Task 3: `t3` CLI Entry Point

**Files:**

- Create: `scripts/t3_cli.py`
- Test: `tests/test_t3_cli.py`

### Step 1: Write the failing test

```python
# tests/test_t3_cli.py
"""Tests for the t3 CLI."""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def ticket_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a minimal ticket dir environment."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))

    main = ws / "my-project"
    main.mkdir()
    (main / ".git").mkdir()

    td = ws / "ac-1234"
    td.mkdir()
    wt = td / "my-project"
    wt.mkdir()

    monkeypatch.setenv("TICKET_DIR", str(td))
    monkeypatch.setenv("_T3_ORIG_CWD", str(wt))
    return td


import pytest


class TestStatusCommand:
    def test_status_json_output(self, ticket_env: Path) -> None:
        from scripts.t3_cli import app  # adjust import path as needed

        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["state"] == "created"

    def test_status_human_output(self, ticket_env: Path) -> None:
        from scripts.t3_cli import app

        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "State:" in result.stdout
        assert "created" in result.stdout


class TestDiagramCommand:
    def test_diagram_outputs_mermaid(self) -> None:
        from scripts.t3_cli import app

        result = runner.invoke(app, ["diagram"])
        assert result.exit_code == 0
        assert "stateDiagram-v2" in result.stdout
        assert "created --> provisioned" in result.stdout
```

### Step 2: Run test to verify it fails

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_t3_cli.py -x`
Expected: FAIL — import error

### Step 3: Write minimal implementation

```python
#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""t3 — Worktree lifecycle CLI.

Both users and AI agents call this CLI. It wraps the state machine
and extension point registry, providing deterministic infrastructure
operations with structured output.
"""

import json
import sys

import lib.init
import typer

lib.init.init()

from lib.env import detect_ticket_dir
from lib.fsm import generate_mermaid, get_available_transitions
from lib.lifecycle import WorktreeLifecycle

app = typer.Typer(
    name="t3",
    help="Worktree lifecycle manager",
    add_completion=False,
    no_args_is_help=True,
)


def _get_lifecycle() -> WorktreeLifecycle:
    td = detect_ticket_dir()
    if not td:
        print("Error: not in a ticket directory", file=sys.stderr)
        raise typer.Exit(1)
    return WorktreeLifecycle(ticket_dir=td)


@app.command()
def status(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show current worktree state, ports, DB, and available transitions."""
    lc = _get_lifecycle()
    data = lc.status()
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"State: {data['state']}")
        if data["facts"].get("ports"):
            ports = data["facts"]["ports"]
            print(f"  Backend:  http://localhost:{ports['backend']}")
            print(f"  Frontend: http://localhost:{ports['frontend']}")
            print(f"  Postgres: localhost:{ports['postgres']}")
        if data["facts"].get("db_name"):
            print(f"  Database: {data['facts']['db_name']}")
        print("Available transitions:")
        for t in data["available_transitions"]:
            conds = f" (requires: {', '.join(t['conditions'])})" if t["conditions"] else ""
            print(f"  t3 {t['method'].replace('_', '-')}{conds}")


@app.command()
def diagram() -> None:
    """Print the lifecycle state diagram as Mermaid."""
    print(generate_mermaid(WorktreeLifecycle))


@app.command()
def setup(
    variant: str = typer.Argument("", help="Tenant variant"),
    ticket_url: str = typer.Option("", help="Ticket URL"),
) -> None:
    """Provision worktree: ports, env, symlinks, DB."""
    lc = _get_lifecycle()
    from lib.env import resolve_context

    ctx = resolve_context()
    lc.provision(wt_dir=ctx.wt_dir, main_repo=ctx.main_repo, variant=variant)
    print(json.dumps(lc.status(), indent=2, default=str))


@app.command()
def start() -> None:
    """Start dev servers (backend + frontend)."""
    lc = _get_lifecycle()
    lc.start_services()
    lc.verify()
    print(json.dumps(lc.status(), indent=2, default=str))


@app.command(name="db-refresh")
def db_refresh() -> None:
    """Re-import database from dump/DSLR."""
    lc = _get_lifecycle()
    lc.db_refresh()
    print(json.dumps(lc.status(), indent=2, default=str))


@app.command()
def clean() -> None:
    """Teardown worktree — stop services, drop DB, clean state."""
    lc = _get_lifecycle()
    lc.teardown()
    print("Worktree cleaned")


if __name__ == "__main__":
    app()
```

### Step 4: Run test to verify it passes

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_t3_cli.py -x -v`
Expected: PASS

### Step 5: Commit

```bash
cd ~/workspace/souliane/teatree
git add scripts/t3_cli.py tests/test_t3_cli.py
git commit -m "feat: add t3 CLI with status, diagram, setup, start, db-refresh, clean"
```

---

## Task 4: Wire CLI into Bootstrap (Backward Compat)

**Files:**

- Modify: `scripts/lib/bootstrap.sh`
- No test needed (shell integration, verified manually)

### Step 1: Add `t3` function to bootstrap.sh

Add at the end of `bootstrap.sh`:

```bash
# t3 CLI — unified entry point (state machine + lifecycle)
function t3 { _t3_py t3_cli.py "$@"; }
```

The existing `t3_*` functions remain for backward compatibility. They will gradually be replaced as users adopt `t3 <command>`.

### Step 2: Verify shell loading

Run: `source ~/workspace/souliane/teatree/scripts/lib/bootstrap.sh && type t3`
Expected: `t3 is a function`

### Step 3: Commit

```bash
cd ~/workspace/souliane/teatree
git add scripts/lib/bootstrap.sh
git commit -m "feat: wire t3 CLI into bootstrap.sh for shell access"
```

---

## Task 5: Integration Test — Full Lifecycle

**Files:**

- Create: `tests/test_lifecycle_integration.py`

### Step 1: Write integration test

```python
# tests/test_lifecycle_integration.py
"""Integration test: full lifecycle from created → provisioned → teardown."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.extension_points import register_defaults
from lib.lifecycle import WorktreeLifecycle


@pytest.fixture
def full_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Set up workspace with main repo, ticket dir, worktree."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))

    main = ws / "my-project"
    main.mkdir()
    (main / ".git").mkdir()

    td = ws / "ac-1234"
    td.mkdir()
    wt = td / "my-project"
    wt.mkdir()

    monkeypatch.setenv("TICKET_DIR", str(td))
    monkeypatch.setenv("_T3_ORIG_CWD", str(wt))

    register_defaults()

    return {"workspace": ws, "main": main, "ticket_dir": td, "wt": wt}


class TestFullLifecycle:
    @patch("lib.lifecycle.db_exists", return_value=True)
    @patch("lib.lifecycle.find_free_ports", return_value=(8005, 4205, 5437, 6379))
    @patch("lib.lifecycle.registry")
    def test_created_to_provisioned_to_teardown(
        self,
        mock_registry: MagicMock,
        mock_ports: MagicMock,
        mock_db: MagicMock,
        full_workspace: dict,
    ) -> None:
        td = full_workspace["ticket_dir"]
        lc = WorktreeLifecycle(ticket_dir=str(td))

        # Initial state
        assert lc.state == "created"
        status = lc.status()
        assert any(t["method"] == "provision" for t in status["available_transitions"])
        assert not any(t["method"] == "start_services" for t in status["available_transitions"])

        # Provision
        lc.provision(
            wt_dir=str(full_workspace["wt"]),
            main_repo=str(full_workspace["main"]),
            variant="acme",
        )
        assert lc.state == "provisioned"
        assert lc.facts["ports"]["backend"] == 8005
        assert lc.facts["variant"] == "acme"

        # State file persisted
        state_file = td / ".state.json"
        assert state_file.is_file()
        data = json.loads(state_file.read_text())
        assert data["state"] == "provisioned"

        # Reload from disk
        lc2 = WorktreeLifecycle(ticket_dir=str(td))
        assert lc2.state == "provisioned"
        assert lc2.facts["ports"]["backend"] == 8005

        # Teardown
        lc2.teardown()
        assert lc2.state == "created"
        assert not state_file.is_file()
```

### Step 2: Run test

Run: `cd ~/workspace/souliane/teatree && uv run pytest tests/test_lifecycle_integration.py -x -v`
Expected: PASS

### Step 3: Commit

```bash
cd ~/workspace/souliane/teatree
git add tests/test_lifecycle_integration.py
git commit -m "test: add integration test for full worktree lifecycle"
```

---

## Task 6: Run Full Test Suite + Pre-Commit

### Step 1: Run all tests

Run: `cd ~/workspace/souliane/teatree && uv run pytest -x`
Expected: PASS with 100% coverage

### Step 2: Run pre-commit

Run: `cd ~/workspace/souliane/teatree && prek run --all-files`
Expected: PASS

### Step 3: Fix any issues found

If coverage is below 100%, add missing test cases. If ruff/ty flags issues, fix them.

### Step 4: Final commit if needed

```bash
cd ~/workspace/souliane/teatree
git add -u
git commit -m "fix: address coverage gaps and linting"
```

---

## Summary of Changes

| File | Action | Purpose |
|------|--------|---------|
| `scripts/lib/fsm.py` | Create | `@transition` decorator, introspection, mermaid generation |
| `scripts/lib/lifecycle.py` | Create | `WorktreeLifecycle` state machine wrapping existing infrastructure |
| `scripts/t3_cli.py` | Create | `t3` CLI entry point (status, diagram, setup, start, db-refresh, clean) |
| `scripts/lib/bootstrap.sh` | Modify | Add `t3` function (1 line) |
| `tests/test_fsm.py` | Create | Decorator tests (9 cases) |
| `tests/test_lifecycle.py` | Create | State machine unit tests |
| `tests/test_lifecycle_integration.py` | Create | Full lifecycle integration test |
| `tests/test_t3_cli.py` | Create | CLI command tests |

## What This Does NOT Change

- Existing `t3_*` shell functions remain (backward compat)
- Existing `wt_setup.py` remains (the lifecycle calls the same functions)
- Extension point registry unchanged
- ac-oper overlay unchanged (still registers at project layer)
- `.env.worktree` format unchanged (state machine adds `.state.json` alongside it)

## Future Tasks (Not in This Plan)

- Shrink skill prose in `t3-workspace/SKILL.md` to reference CLI
- Update ac-oper's `bootstrap.sh` to override `t3 start` with tenant detection
- Add `t3 transitions` command for the status line snippet we discussed
- Add process tracking (PIDs, container IDs) to facts for `t3 status`
- Remove `t3_*` shell functions once CLI adoption is complete
