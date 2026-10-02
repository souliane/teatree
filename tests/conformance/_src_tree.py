"""Parsed-AST index of the source tree, memoised for the whole conformance lane.

Every lane here is an introspective walk whose INPUT is the whole tree, and most
of them assert several derived views of it. Parsing is a pure function of a tree
that cannot change inside a run, so an uncached lane re-reads and re-parses the
same ~1600 modules once per assertion — the dominant cost of the push gate's
conformance step, and the reason its slowest assertion sat on the 60s
``pytest-timeout`` ceiling on a loaded host.

``test_cross_tier_artifact_parity`` already memoises its own walk for exactly
this reason; this module is that idiom hoisted so the whole package shares ONE
parse per root per process instead of one per lane.

Strict on purpose: an unreadable or unparsable module under a scanned root is a
real defect, not something to skip past silently.
"""

import ast
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src" / "teatree"


@cache
def parsed_modules(root: Path) -> tuple[tuple[Path, ast.Module], ...]:
    """Every ``*.py`` under *root* as ``(path, parsed AST)``, parsed once per process."""
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))) for path in sorted(root.rglob("*.py"))
    )


def src_modules() -> tuple[tuple[Path, ast.Module], ...]:
    """The memoised parse of the whole ``src/teatree`` package."""
    return parsed_modules(SRC_DIR)
