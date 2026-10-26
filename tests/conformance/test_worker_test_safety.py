import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import conftest


def _uncontrolled_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) == "resurrect_worker"
        and not {"flock_free", "spawn"} <= {keyword.arg for keyword in node.keywords}
    )


def _uncontrolled_resurrections(tree: ast.AST) -> list[int]:
    offenders = []
    for node in ast.walk(tree):
        if _uncontrolled_call(node):
            offenders.append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "resurrect_" + "worker(" in node.value:
            try:
                payload = ast.parse(node.value)
            except SyntaxError:
                offenders.append(node.lineno)
            else:
                if any(_uncontrolled_call(call) for call in ast.walk(payload)):
                    offenders.append(node.lineno)
    return offenders


def test_tests_do_not_call_real_worker_resurrection() -> None:
    tests_root = Path(__file__).parents[1]
    offenders = {
        str(path.relative_to(tests_root)): _uncontrolled_resurrections(ast.parse(path.read_text(encoding="utf-8")))
        for path in tests_root.rglob("test_*.py")
    }
    assert not {path: lines for path, lines in offenders.items() if lines}
    assert _uncontrolled_resurrections(ast.parse("resurrect_" + "worker()")) == [1]


def test_python_payloads_require_both_worker_resurrection_stubs() -> None:
    bare = "resurrect_" + "worker()"
    one_stub = "resurrect_" + "worker(flock_free=lambda: True)"
    both_stubs = "resurrect_" + "worker(flock_free=lambda: True, spawn=lambda: None)"
    assert _uncontrolled_resurrections(ast.parse(repr(bare))) == [1]
    assert _uncontrolled_resurrections(ast.parse(repr(one_stub))) == [1]
    assert _uncontrolled_resurrections(ast.parse(repr(both_stubs))) == []


@pytest.mark.parametrize("binary", ["/nonexistent/t3", b"/nonexistent/t3"])
def test_real_worker_process_spawn_is_refused(binary: str | bytes) -> None:
    with pytest.raises(pytest.fail.Exception, match="real t3 worker"):
        subprocess.Popen([binary, "worker"])


def test_isolation_replaces_inherited_control_plane_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("T3_CONTROL_DB_DIR", "T3_DATA_DIR", "T3_WORKER_HEALTH_DIR"):
        monkeypatch.setenv(name, "/var/lib/teatree/production")
    conftest._isolate_env.__wrapped__(monkeypatch, tmp_path)
    expected = tmp_path / "isolated-control-db"
    assert not expected.exists()
    child = subprocess.run(
        [sys.executable, "-I", "-c", "import os; print(os.environ['T3_CONTROL_DB_DIR'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == str(expected)
    assert all(name not in os.environ for name in ("T3_DATA_DIR", "T3_WORKER_HEALTH_DIR"))
