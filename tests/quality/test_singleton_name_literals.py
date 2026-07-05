"""Tree-wide guard: worker-singleton names are the imported constants, never literals (#5).

The drain-vs-worker singleton drift (``"teatree-worker"`` probe vs ``"worker"`` acquire)
is the slug/singleton identity-normalization family: a name normalized to a constant at
one site and a raw literal at another. The import-level parity (the probe importing the
same ``WORKER_SINGLETON`` the worker acquires) is the primary defence; this lint is the
backstop, asserting no raw singleton-name literal survives at a ``singleton`` /
``default_pid_path`` / ``flock_is_held`` call site, and that the legacy ``teatree-worker``
name exists as a literal only in its constant's definition home.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

from teatree.utils.singleton import LEGACY_WORKER_SINGLETON, WORKER_SINGLETON

_SRC = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The singleton-name APIs whose first positional arg is a singleton name.
_SINGLETON_NAME_APIS = {"singleton", "default_pid_path", "flock_is_held"}
#: The canonical singleton names that must flow through the constants, never a literal.
_SINGLETON_NAMES = {"worker", "teatree-worker"}
#: The one file allowed to hold the raw ``teatree-worker`` literal — where the constant lives.
_CONSTANT_HOME = _SRC / "utils" / "singleton.py"


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


def _exact_legacy_literals(tree: ast.AST) -> list[int]:
    """Line numbers of every exact ``"teatree-worker"`` string constant (sorted)."""
    return sorted(
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == "teatree-worker"
    )


def _iter_src() -> list[tuple[Path, ast.AST]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(_SRC.rglob("*.py"))]


class TestNoSingletonNameLiterals:
    def test_constants_are_the_canonical_singleton_names(self) -> None:
        assert WORKER_SINGLETON == "worker"
        assert LEGACY_WORKER_SINGLETON == "teatree-worker"

    def test_no_singleton_name_literal_at_a_singleton_api_call(self) -> None:
        offenders: dict[str, list[tuple[int, str]]] = {}
        for path, tree in _iter_src():
            hits = _singleton_literal_calls(tree)
            if hits:
                offenders[str(path.relative_to(_SRC))] = hits
        assert offenders == {}, (
            "singleton names must be the imported WORKER_SINGLETON / LEGACY_WORKER_SINGLETON "
            f"constants, never a string literal at a singleton()/default_pid_path()/flock_is_held() call: {offenders}"
        )

    def test_legacy_literal_lives_only_in_the_constant_home(self) -> None:
        offenders: dict[str, list[int]] = {}
        for path, tree in _iter_src():
            if path == _CONSTANT_HOME:
                continue
            hits = _exact_legacy_literals(tree)
            if hits:
                offenders[str(path.relative_to(_SRC))] = hits
        assert offenders == {}, (
            "the legacy 'teatree-worker' name must be referenced through the "
            f"LEGACY_WORKER_SINGLETON constant, never re-spelled as a raw literal: {offenders}"
        )

    def test_detector_has_teeth(self) -> None:
        bad = ast.parse('singleton("worker")\ndefault_pid_path("teatree-worker")\nx = "teatree-worker"\n')
        assert len(_singleton_literal_calls(bad)) == 2
        assert _exact_legacy_literals(bad) == [2, 3]
