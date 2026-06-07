"""Entry-point bootstrap for the ``t3`` console script.

Pins the teatree source to run against the *configured* tree, independent of
cwd.  When ``T3_REPO`` names a teatree source tree (a pyproject with
``name = "teatree"`` and a populated ``src/teatree`` layout), prepend that
tree's ``src`` to ``sys.path``.  Otherwise fall through to whatever the
interpreter already has on ``sys.path``: the uv-tool editable install (main
clone) or the PyPI package when not installed editable.

cwd is never consulted (souliane/teatree#2055): a ``t3`` subprocess whose cwd
lands inside a sibling checkout — e.g. an autonomous ``t3 loop tick`` started
from a feature worktree — must not silently import that checkout's unreviewed
``src/`` against the real shared DB and connectors.  Dogfooding a worktree's
teatree changes is the explicit ``T3_REPO`` opt-in (or a ``uv run`` prefix),
never an accident of where the process happened to start.

This module MUST NOT import anything from ``teatree`` at module scope — the
whole point is to adjust ``sys.path`` BEFORE the ``teatree`` package is loaded.
"""

import os
import sys
from pathlib import Path


def _is_teatree_source(repo: Path) -> bool:
    """Return whether *repo* is a teatree source tree (pyproject + ``src/teatree``)."""
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    if 'name = "teatree"' not in content:
        return False
    return (repo / "src" / "teatree" / "__init__.py").is_file()


def _resolve_pinned_source() -> Path | None:
    """Return ``$T3_REPO/src`` when ``T3_REPO`` names a teatree source tree, else ``None``."""
    env_path = os.environ.get("T3_REPO", "")
    if not env_path:
        return None
    repo = Path(env_path).expanduser()
    if not _is_teatree_source(repo):
        return None
    return repo / "src"


def main() -> None:
    source = _resolve_pinned_source()
    if source is not None:
        sys.path.insert(0, str(source))
    from teatree.cli import main as _main

    _main()


if __name__ == "__main__":
    main()
