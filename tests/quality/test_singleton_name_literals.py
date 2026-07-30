"""Tree-wide guard: the worker-singleton name is the imported constant, never a literal (#5).

The drain-vs-worker singleton drift (a name normalized to a constant at one site and a
raw literal at another) is the slug/singleton identity-normalization family. The
import-level parity (the probe importing the same ``WORKER_SINGLETON`` the worker
acquires) is the primary defence; this lint is the backstop, asserting no raw
singleton-name literal survives at a ``singleton`` / ``default_pid_path`` /
``flock_is_held`` call site. PR-28 completed the #5 deprecation — the legacy
``teatree-worker`` singleton is gone, so there is one canonical name to protect.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

from teatree.utils.singleton import WORKER_SINGLETON

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The singleton-name APIs whose first positional arg is a singleton name.
_SINGLETON_NAME_APIS = {"singleton", "default_pid_path", "flock_is_held"}
#: The canonical singleton names that must flow through the constant, never a literal.
_SINGLETON_NAMES = {"worker"}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _singleton_literal_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, name)`` for every singleton-API call whose first arg is a name literal."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in _SINGLETON_NAME_APIS:
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and first.value in _SINGLETON_NAMES:
                hits.append((node.lineno, first.value))
    return hits


def _iter_src() -> list[tuple[Path, ast.AST]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(_SRC.rglob("*.py"))]


class TestNoSingletonNameLiterals:
    def test_constant_is_the_canonical_singleton_name(self) -> None:
        assert WORKER_SINGLETON == "worker"

    def test_no_singleton_name_literal_at_a_singleton_api_call(self) -> None:
        offenders: dict[str, list[tuple[int, str]]] = {}
        for path, tree in _iter_src():
            hits = _singleton_literal_calls(tree)
            if hits:
                offenders[str(path.relative_to(_SRC))] = hits
        assert offenders == {}, (
            "the worker-singleton name must be the imported WORKER_SINGLETON constant, "
            f"never a string literal at a singleton()/default_pid_path()/flock_is_held() call: {offenders}"
        )

    def test_detector_has_teeth(self) -> None:
        bad = ast.parse('singleton("worker")\ndefault_pid_path("worker")\nx = "worker"\n')
        # The two call-site literals are flagged; a bare assignment is not a call site.
        assert len(_singleton_literal_calls(bad)) == 2
