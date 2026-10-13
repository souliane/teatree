"""No production caller hands the settings interchange its own scan terms.

``export_db_to_toml`` and ``import_toml_to_db`` take ``scan_terms=`` so a test can name its
own placeholder list; a production caller passing one — ``scan_terms=()`` above all — would
skip the resolver that refuses a store holding no ban-class term (souliane/teatree#5101).
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import SRC_DIR, src_modules


def _scan_terms_callers(modules: tuple[tuple[Path, ast.Module], ...]) -> list[str]:
    return [
        f"{path.relative_to(SRC_DIR)}:{node.lineno}"
        for path, tree in modules
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and any(keyword.arg == "scan_terms" for keyword in node.keywords)
    ]


def test_no_source_module_passes_scan_terms() -> None:
    assert _scan_terms_callers(src_modules()) == []


def test_the_walk_sees_a_call_passing_scan_terms() -> None:
    planted = ast.parse("export_db_to_toml(privacy=privacy, scan_terms=())")
    assert _scan_terms_callers(((SRC_DIR / "planted.py", planted),)) == ["planted.py:1"]
