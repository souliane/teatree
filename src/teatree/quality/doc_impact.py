"""Documentation-impact classification for test selection (#3645).

A markdown / docs-tree / mkdocs path carries no executable semantics: nothing
imports it, no doctest reads it, no ast-grep rule matches it. Escalating such a
path to a whole-tree run measured 59m32s / 30182 tests for a one-module fix,
because the blueprint-sync gate REQUIRES a ``BLUEPRINT.md`` edit on exactly the
commits the selector then refuses to scope.

Docs are not zero-impact though — this repo has doc-consistency tests that READ
those files. So the impact is mapped rather than assumed away: a changed doc
expands to the tests whose source names it (by full path, basename, or any
containing directory), the textual analogue of the import scan that maps src
modules to their tests. The module is a pure leaf (stdlib + filesystem only) so
it stays importable from the dependency-light quality lane.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from teatree.quality.test_path_mirror import collect_test_files

DOC_SUFFIXES: tuple[str, ...] = (".md", ".markdown")
DOC_TREE_PREFIXES: tuple[str, ...] = ("docs/",)
DOC_CONFIG_FILES: frozenset[str] = frozenset({"mkdocs.yml", "mkdocs.yaml"})

#: Roots whose non-python files are runtime fixture data a test may parse — the
#: shared classifier already forces FULL for them, and that stays.
_CODE_ROOTS: tuple[str, ...] = ("src/", "tests/")
_PYTHON_SUFFIXES: tuple[str, ...] = (".py", ".pyi")


def is_doc_path(path: str) -> bool:
    """True when *path* is documentation with no executable semantics."""
    if path.startswith(_CODE_ROOTS) or path.endswith(_PYTHON_SUFFIXES):
        return False
    if path in DOC_CONFIG_FILES:
        return True
    return path.startswith(DOC_TREE_PREFIXES) or path.endswith(DOC_SUFFIXES)


def reference_tokens(paths: Iterable[str]) -> frozenset[str]:
    """Every literal a test could use to reach one of *paths*.

    The full posix path, the basename, and each containing directory prefix — a
    test that resolves ``REPO_ROOT / "docs" / "generated" / x`` names none of the
    joined forms but does name ``"docs"``, so directory prefixes keep the map
    over-selecting rather than under-selecting.
    """
    tokens: set[str] = set()
    for path in paths:
        parts = path.split("/")
        tokens.add(path)
        tokens.add(parts[-1])
        tokens.update("/".join(parts[:index]) + "/" for index in range(1, len(parts)))
    return frozenset(tokens)


def disk_doc_reader_lookup(root: Path) -> Callable[[frozenset[str]], tuple[str, ...]]:
    """A resolver: doc reference tokens → the test files whose source names any of them."""

    def lookup(tokens: frozenset[str]) -> tuple[str, ...]:
        if not tokens:
            return ()
        readers: list[str] = []
        for path in collect_test_files(root):
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(token in source for token in tokens):
                readers.append(path.relative_to(root).as_posix())
        return tuple(sorted(readers))

    return lookup
