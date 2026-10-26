import ast
import os
import subprocess
from pathlib import Path

import pytest

from tests import conftest


def _uncontrolled_resurrections(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr) == "resurrect_worker"
        and not {"flock_free", "spawn"} <= {keyword.arg for keyword in node.keywords}
    ]


def test_tests_do_not_call_real_worker_resurrection() -> None:
    tests_root = Path(__file__).parents[1]
    offenders = {
        str(path.relative_to(tests_root)): _uncontrolled_resurrections(ast.parse(path.read_text(encoding="utf-8")))
        for path in tests_root.rglob("test_*.py")
    }
    assert not {path: lines for path, lines in offenders.items() if lines}
    assert _uncontrolled_resurrections(ast.parse("resurrect_worker()")) == [1]


@pytest.mark.parametrize("binary", ["/nonexistent/t3", b"/nonexistent/t3"])
def test_real_worker_process_spawn_is_refused(binary: str | bytes) -> None:
    with pytest.raises(pytest.fail.Exception, match="real t3 worker"):
        subprocess.Popen([binary, "worker"])


def test_isolation_scrubs_inherited_control_plane_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("T3_CONTROL_DB_DIR", "T3_DATA_DIR", "T3_WORKER_HEALTH_DIR"):
        monkeypatch.setenv(name, "/var/lib/teatree/production")
    conftest._isolate_env.__wrapped__(monkeypatch, tmp_path)
    assert all(name not in os.environ for name in ("T3_CONTROL_DB_DIR", "T3_DATA_DIR", "T3_WORKER_HEALTH_DIR"))
