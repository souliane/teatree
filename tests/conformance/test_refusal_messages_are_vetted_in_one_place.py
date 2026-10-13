"""One method decides whether an import refusal may quote its message (souliane/teatree#5101).

A parser error or a preset-mask refusal quotes the file's values, the box's loop names or a
fixed list of valid values, and any of them can be a banned term. Three copies of that rule
once disagreed, so a settings refusal kept a message the seed path had learned to withhold.
"""

import ast
from collections.abc import Callable
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, src_modules

_POLICY_METHOD = "core/config_interchange/import_rows.py:ImportPolicy.refusal"
_IMPORT_ROWS = SRC_DIR / "core" / "config_interchange" / "import_rows.py"

type _Predicate = Callable[[ast.AST], bool]


def _withholds_a_message(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and "message withheld" in node.value


def _scans_for_terms(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "line_matches"


def _scopes(tree: ast.AST, predicate: _Predicate, enclosing: tuple[str, ...] = ()) -> set[str]:
    found = {".".join(enclosing) or "<module>"} if predicate(tree) else set()
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found |= _scopes(child, predicate, (*enclosing, child.name))
        else:
            found |= _scopes(child, predicate, enclosing)
    return found


def _withholding_scopes(modules: tuple[tuple[Path, ast.Module], ...]) -> set[str]:
    return {
        f"{path.relative_to(SRC_DIR)}:{scope}"
        for path, tree in modules
        for scope in _scopes(tree, _withholds_a_message)
    }


def test_one_method_withholds_a_refusal_message() -> None:
    assert _withholding_scopes(src_modules()) == {_POLICY_METHOD}


def test_the_import_rows_scan_text_for_terms_only_inside_the_policy() -> None:
    scopes = _scopes(ast.parse(_IMPORT_ROWS.read_text(encoding="utf-8")), _scans_for_terms)
    assert {scope for scope in scopes if not scope.startswith("ImportPolicy.")} == set()


def test_the_guards_see_a_second_copy_of_the_rule() -> None:
    planted = ast.parse('def vet(reason):\n    return "message withheld" if line_matches(reason, t) else reason\n')
    assert _withholding_scopes(((SRC_DIR / "planted.py", planted),)) == {"planted.py:vet"}
    assert _scopes(planted, _scans_for_terms) == {"vet"}
