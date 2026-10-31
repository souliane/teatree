"""Every import-time binding of the data directory is covered by the per-test isolation roster.

``teatree.paths.DATA_DIR`` and ``CANONICAL_DB`` are resolved once at import. A module that copies
either (``from teatree.paths import DATA_DIR`` or a constant derived from one) keeps the host's
value after a test patches ``teatree.paths``, so the test reads and writes the real data directory.
The census walks ``src/teatree`` and fails on a binder the roster does not patch, on a roster
entry that no longer binds anything, and on a roster that ``tests/conftest.py`` does not apply.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

from tests._data_dir_binders import DATA_DIR_BINDERS, EXEMPT_MODULES

_TESTS = Path(__file__).resolve().parents[1]
_SRC = _TESTS.parent / "src" / "teatree"
_BOUND_NAMES = frozenset({"DATA_DIR", "CANONICAL_DB"})
_DEFINING_MODULE = "teatree.paths"


def _references_bound_name(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in _BOUND_NAMES:
            return True
        if isinstance(child, ast.Attribute) and child.attr in _BOUND_NAMES:
            return True
    return False


def _assigned_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def binders_in_source(source: str, module: str) -> set[str]:
    """Return ``"<module>:<name>"`` for each module-level name that captures the data directory at import."""
    found: set[str] = set()
    for node in ast.parse(source, filename=module).body:
        if isinstance(node, ast.ImportFrom) and node.module == _DEFINING_MODULE and node.level == 0:
            found.update(f"{module}:{alias.asname or alias.name}" for alias in node.names if alias.name in _BOUND_NAMES)
        elif (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and _references_bound_name(node.value)
        ):
            found.update(f"{module}:{name}" for name in _assigned_names(node))
    return found


def discover_data_dir_binders() -> set[str]:
    found: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        parts = path.relative_to(_SRC.parent).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if module != _DEFINING_MODULE:
            found |= binders_in_source(path.read_text(encoding="utf-8"), module)
    return found


def _is_exempt(binder: str) -> bool:
    return binder.split(":", 1)[0] in EXEMPT_MODULES


class TestDiscovery:
    def test_a_from_import_of_the_data_dir_is_a_binder(self) -> None:
        source = "from teatree.paths import DATA_DIR, CANONICAL_DB as DB\n"

        assert binders_in_source(source, "pkg.mod") == {"pkg.mod:DATA_DIR", "pkg.mod:DB"}

    def test_a_constant_derived_from_the_data_dir_is_a_binder(self) -> None:
        source = "from teatree import paths\nLOG = paths.DATA_DIR / 'x.log'\nOTHER = 3\n"

        assert binders_in_source(source, "pkg.mod") == {"pkg.mod:LOG"}

    def test_a_call_time_lookup_is_not_a_binder(self) -> None:
        source = "def where():\n    from teatree.paths import DATA_DIR\n    return DATA_DIR\n"

        assert binders_in_source(source, "pkg.mod") == set()


class TestRosterCoversTheTree:
    def test_every_import_time_binder_is_patched_or_exempt(self) -> None:
        uncovered = sorted(
            binder
            for binder in discover_data_dir_binders()
            if binder not in DATA_DIR_BINDERS and not _is_exempt(binder)
        )

        assert not uncovered, (
            "These modules bind the data directory at import and are not isolated per test; add each to "
            f"DATA_DIR_BINDERS in tests/_data_dir_binders.py, or EXEMPT_MODULES with a reason: {uncovered}"
        )

    def test_no_roster_entry_is_stale(self) -> None:
        discovered = discover_data_dir_binders()

        stale = sorted(binder for binder in DATA_DIR_BINDERS if binder not in discovered)

        assert not stale, f"These roster entries bind nothing any more; remove them: {stale}"

    def test_no_exempt_module_is_stale(self) -> None:
        modules = {binder.split(":", 1)[0] for binder in discover_data_dir_binders()}

        assert not sorted(set(EXEMPT_MODULES) - modules)

    def test_no_binder_is_both_patched_and_exempt(self) -> None:
        assert not sorted(binder for binder in DATA_DIR_BINDERS if _is_exempt(binder))

    def test_conftest_applies_the_roster_to_every_test(self) -> None:
        conftest = (_TESTS / "conftest.py").read_text(encoding="utf-8")

        assert "isolate_data_dir_binders(monkeypatch" in conftest
