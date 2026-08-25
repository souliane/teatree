"""Every ``@task`` callable declares how its result reports failure (#4528).

The runtime contract only holds for callables that route through
``teatree.core.task_contract.task``. Both halves drift silently otherwise: a new
``@task`` importing ``django.tasks.task`` directly is back to "returned failure
reads as SUCCESSFUL", and one that reaches the teatree decorator without an
``outcome=`` fails at import rather than in review. This walk is the AST guard
for both, so a NEW task enrolls or the PR that adds it turns red.
"""

import ast
from pathlib import Path

import pytest

from teatree.core.task_contract import TaskOutcome
from tests.conformance._src_tree import SRC_DIR, src_modules

#: The one module allowed to reach ``django.tasks.task`` — it IS the contract.
_CONTRACT_MODULE = Path("core/task_contract.py")


def _task_decorators(tree: ast.Module) -> list[tuple[str, ast.Call | ast.Name]]:
    """``(function name, decorator node)`` for every ``@task`` / ``@task(...)`` in *tree*."""
    found: list[tuple[str, ast.Call | ast.Name]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name) and target.id == "task":
                found.append((node.name, decorator))
    return found


def _direct_task_importers() -> list[str]:
    offenders = []
    for path, tree in src_modules():
        if path.relative_to(SRC_DIR) == _CONTRACT_MODULE:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "django.tasks":
                offenders.extend(f"{path.relative_to(SRC_DIR)}" for alias in node.names if alias.name == "task")
    return sorted(offenders)


def _declared_task_decorators() -> list[tuple[str, str, ast.Call | ast.Name]]:
    return [
        (str(path.relative_to(SRC_DIR)), name, decorator)
        for path, tree in src_modules()
        if path.relative_to(SRC_DIR) != _CONTRACT_MODULE
        for name, decorator in _task_decorators(tree)
    ]


def test_only_the_contract_module_imports_django_tasks_task() -> None:
    offenders = _direct_task_importers()

    assert not offenders, (
        f"{offenders} import `task` from django.tasks directly, bypassing the outcome contract. "
        "Import it from teatree.core.task_contract instead (#4528)."
    )


def test_the_walk_finds_the_task_callables_it_is_guarding() -> None:
    """Anti-vacuity: a walk that matched nothing would pass both lanes below."""
    assert len(_declared_task_decorators()) >= 20


@pytest.mark.parametrize(
    ("module", "name", "decorator"),
    [pytest.param(m, n, d, id=f"{m}::{n}") for m, n, d in _declared_task_decorators()],
)
def test_every_task_declares_a_known_outcome(module: str, name: str, decorator: ast.expr) -> None:
    assert isinstance(decorator, ast.Call), f"{module}::{name}: bare @task carries no outcome= (#4528)"
    declared = [kw.value for kw in decorator.keywords if kw.arg == "outcome"]
    assert declared, f"{module}::{name}: @task(...) carries no outcome= (#4528)"

    value = declared[0]
    where = f"{module}::{name}: outcome= must name a TaskOutcome member"
    assert isinstance(value, ast.Attribute), f"{where}, got {ast.dump(value)}"
    assert isinstance(value.value, ast.Name), f"{where}, got {ast.dump(value)}"
    assert value.value.id == "TaskOutcome", f"{where}, got {value.value.id}"
    assert value.attr in TaskOutcome.__members__, f"{where}, got TaskOutcome.{value.attr}"
