"""Hook launches must not target commands that always refuse their work."""

import ast
from pathlib import Path

from django.core.management import get_commands
from typer.main import get_command

from teatree.cli import app

_HOOK_SCRIPTS = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
_REFUSAL_ONLY = {"speak"}


def test_hook_launches_do_not_target_refusal_only_commands() -> None:
    launches: set[str] = set()
    for path in _HOOK_SCRIPTS.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, (ast.Name, ast.Attribute)):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if name == "t3_argv" and node.args and isinstance(node.args[0], ast.Constant):
                    if isinstance(node.args[0].value, str):
                        launches.add(node.args[0].value)
                elif name == "spawn_t3_detached" and node.args and isinstance(node.args[0], ast.List):
                    argv = node.args[0].elts
                    if len(argv) > 1 and isinstance(argv[1], ast.Constant) and isinstance(argv[1].value, str):
                        launches.add(argv[1].value)
    assert {"tool", "teatree"} <= launches
    assert not launches & _REFUSAL_ONLY


def test_refusal_only_speak_entry_points_are_absent() -> None:
    assert "speak" not in get_command(app).commands
    assert "speak" not in get_commands()
