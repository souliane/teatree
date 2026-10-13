"""Only ``ImportPolicy.refusal`` withholds a refusal message; the interchange scans for terms only where listed."""

import ast
from collections.abc import Iterator
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, src_modules

_INTERCHANGE = "core/config_interchange/"
_TERM_SCANS = frozenset({"line_matches", "withholds_name"})
_ALLOWED_SCANS = {
    f"{_INTERCHANGE}{site}"
    for site in (
        "export_rows.py:ExportGuard.reported_omissions:line_matches",
        "export_rows.py:ExportGuard.withheld_name:line_matches",
        "import_rows.py:ImportPolicy.refusal:line_matches",
        "import_rows.py:ImportPolicy.withholds_name:line_matches",
        "import_rows.py:_seed_rejection:withholds_name",
        "import_rows.py:classify_document:withholds_name",
        "import_rows.py:file_seed_dispositions:withholds_name",
        "import_rows.py:seed_row:withholds_name",
        "secret_guard.py:carries_banned_term:line_matches",
    )
}
_SECOND_COPIES = """
def via_the_name_check(reason, policy):
    return "refused" if policy.withholds_name(reason) else reason

def bare(reason, terms):
    return "refused" if line_matches(reason, terms) else reason

def qualified(reason, policy):
    return "refused" if term_match.line_matches(reason, policy.terms) else reason

class ImportPolicy:
    def seed_message(self, reason):
        return "refused" if line_matches(reason, self.terms or ()) else reason
"""

type _Modules = tuple[tuple[Path, ast.Module], ...]


def _nodes(tree: ast.AST, enclosing: tuple[str, ...] = ()) -> Iterator[tuple[str, ast.AST]]:
    yield ".".join(enclosing) or "<module>", tree
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _nodes(child, (*enclosing, child.name))
        else:
            yield from _nodes(child, enclosing)


def _callee(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _withheld_messages(modules: _Modules) -> set[str]:
    return {
        f"{path.relative_to(SRC_DIR)}:{scope}"
        for path, tree in modules
        for scope, node in _nodes(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "message withheld" in node.value
    }


def _term_scans(modules: _Modules) -> set[str]:
    return {
        f"{path.relative_to(SRC_DIR)}:{scope}:{callee}"
        for path, tree in modules
        if path.relative_to(SRC_DIR).as_posix().startswith(_INTERCHANGE)
        for scope, node in _nodes(tree)
        if (callee := _callee(node)) in _TERM_SCANS
    }


def test_one_method_withholds_a_refusal_message() -> None:
    assert _withheld_messages(src_modules()) == {f"{_INTERCHANGE}import_rows.py:ImportPolicy.refusal"}


def test_the_interchange_scans_for_terms_only_where_listed() -> None:
    assert _term_scans(src_modules()) == _ALLOWED_SCANS


def test_the_guards_see_every_shape_of_a_second_copy() -> None:
    planted = ((SRC_DIR / _INTERCHANGE / "planted.py", ast.parse(_SECOND_COPIES)),)
    assert _term_scans(planted) == {
        f"{_INTERCHANGE}planted.py:{site}"
        for site in (
            "via_the_name_check:withholds_name",
            "bare:line_matches",
            "qualified:line_matches",
            "ImportPolicy.seed_message:line_matches",
        )
    }
    phrase = ((SRC_DIR / "planted.py", ast.parse('def vet():\n    return "message withheld"\n')),)
    assert _withheld_messages(phrase) == {"planted.py:vet"}
