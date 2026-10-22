import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OPTIONS = "SQLITE_WRITE_SERIALIZATION_OPTIONS"
_BOUNDARY = "SQLITE_BOUNDARY_ENGINE"
_STOCK = "django.db.backends.sqlite3"


def _name(node: ast.expr | None) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _expected_refusal(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    while parent is not None and not isinstance(parent, ast.With):
        parent = parents.get(parent)
    return isinstance(parent, ast.With) and any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and _name(item.context_expr.func.value) == "pytest"
        and item.context_expr.func.attr == "raises"
        and any(_name(arg) == "ValueError" for arg in item.context_expr.args)
        for item in parent.items
    )


def _literal_pair(node: ast.Dict) -> bool:
    fields = {
        key.value: value
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    engine = fields.get("ENGINE")
    return _name(fields.get("OPTIONS")) == _OPTIONS and isinstance(engine, ast.Constant) and engine.value == _STOCK


def _call_pairs_stock(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    if not isinstance(node.func, ast.Name):
        return False
    arguments = {keyword.arg: keyword.value for keyword in node.keywords}
    if node.func.id == "_make_alias":
        return any(_name(arg) == _OPTIONS for arg in node.args) and _name(arguments.get("engine")) != _BOUNDARY
    if node.func.id == "register_sqlite_alias":
        return (
            _name(arguments.get("options")) == _OPTIONS
            and _name(arguments.get("engine")) != _BOUNDARY
            and not _expected_refusal(node, parents)
        )
    if node.func.id in {"_databases", "_database_config"} and len(node.args) > 1:
        engine = node.args[1]
        return isinstance(engine, ast.Constant) and engine.value == _STOCK
    return False


def _unsafe_pairs(source: str) -> list[int]:
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Dict) and _literal_pair(node))
        or (isinstance(node, ast.Call) and _call_pairs_stock(node, parents))
    ]


def test_alias_pair_guard_detects_an_unsafe_call_and_literal() -> None:
    source = (
        "_make_alias(tmp_path, SQLITE_WRITE_SERIALIZATION_OPTIONS)\n"
        "{'ENGINE': 'django.db.backends.sqlite3', 'OPTIONS': SQLITE_WRITE_SERIALIZATION_OPTIONS}\n"
        "register_sqlite_alias('x', db, options=SQLITE_WRITE_SERIALIZATION_OPTIONS)\n"
        "_database_config(db, 'django.db.backends.sqlite3')\n"
        "with pytest.raises(ValueError):\n"
        "    register_sqlite_alias('x', db, options=SQLITE_WRITE_SERIALIZATION_OPTIONS)\n"
    )
    assert _unsafe_pairs(source) == [1, 2, 3, 4]


def test_no_test_module_pairs_control_options_with_stock_engine() -> None:
    offenders = []
    for path in _ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if _OPTIONS in source:
            offenders.extend(f"{path.relative_to(_ROOT)}:{line}" for line in _unsafe_pairs(source))
    assert offenders == []
