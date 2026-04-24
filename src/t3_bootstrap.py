"""Entry-point bootstrap for the ``t3`` console script.

Selects the teatree source to run against at invocation time.  When cwd is
inside a teatree source tree (a pyproject with ``name = "teatree"`` and a
populated ``src/teatree`` layout), prepend that worktree's ``src`` to
``sys.path`` so dogfooding a worktree's changes via the global ``t3`` just
works — no ``uv run`` prefix.  Otherwise fall through to whatever the
interpreter already has on ``sys.path``: the uv-tool editable install
(main clone) or the PyPI package when not installed editable.

This module MUST NOT import anything from ``teatree`` at module scope — the
whole point is to adjust ``sys.path`` BEFORE the ``teatree`` package is loaded.
"""

import sys
from pathlib import Path


def _find_teatree_source(start: Path) -> Path | None:
    """Walk up from *start* and return ``<repo>/src`` when the repo is a teatree source tree."""
    for parent in [start, *start.parents]:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            content = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if 'name = "teatree"' not in content:
            continue
        src = parent / "src"
        if (src / "teatree" / "__init__.py").is_file():
            return src
    return None


def main() -> None:
    source = _find_teatree_source(Path.cwd())
    if source is not None:
        sys.path.insert(0, str(source))
    from teatree.cli import main as _main

    _main()


if __name__ == "__main__":
    main()
