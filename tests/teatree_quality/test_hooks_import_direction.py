"""Fitness function: the hook-leaf import direction is ONE-WAY (router → leaf).

``hooks/scripts/hook_router.py`` is the PreToolUse/Stop/SessionStart dispatcher.
A whole concern at a time is being lifted out of the 8800-LOC router into a
leaf under ``src/teatree/hooks/`` (the Slack mirror, #2384; the ~25 scanner
leaves before it). The router imports the leaves; a leaf must NEVER import the
router back. A back-edge (``from hook_router import …`` inside a leaf, top-level
OR lazy) means the "extraction" still depends on router internals — the concern
was not actually self-contained, the router cannot shrink permanently, and the
dependency graph grows a cycle the module-health work exists to remove.

No fitness function governed this before: ``hook_router`` lives OUTSIDE ``src/``
so tach does not see it, and the per-file scan filters to ``src/``. This AST
test closes that gap for the import EDGE the way ``check_module_health.py``
closed it for the LOC.

Two halves, mirroring ``tests/quality/test_project_leaf.py``:

:class:`TestNoLeafImportsRouter` AST-scans every ``src/teatree/hooks/*.py`` leaf
and asserts none imports ``hook_router`` — a copy-pasted ``from hook_router
import _helper`` turns it red.

:class:`TestCheckerIsAntiVacuous` plants a synthetic leaf carrying the back-edge
and proves the same checker flags it (so the live-tree green is meaningful, not
a no-op), and a synthetic clean leaf proves it does NOT over-flag.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOKS_LEAF_DIR = _REPO_ROOT / "src" / "teatree" / "hooks"

# The forbidden back-edge target. Both the bare module (the router runs as a
# script with ``hooks/scripts`` on sys.path) and the dotted subprocess form
# (``hooks.scripts.hook_router``) are back-edges.
_ROUTER_NAMES = {"hook_router", "hooks.scripts.hook_router"}


def _imports_router(source: str, *, filename: str = "<leaf>") -> bool:
    """Whether *source* imports ``hook_router`` in any form (top-level or lazy).

    ``ast.walk`` reaches function-body imports too, so a lazy
    ``from hook_router import …`` inside a handler is caught, not just a
    top-level import.
    """
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _ROUTER_NAMES:
            return True
        if isinstance(node, ast.Import) and any(alias.name in _ROUTER_NAMES for alias in node.names):
            return True
    return False


def _leaf_files() -> list[Path]:
    return sorted(p for p in _HOOKS_LEAF_DIR.glob("*.py") if p.name != "__init__.py")


class TestNoLeafImportsRouter:
    def test_no_hooks_leaf_imports_hook_router(self) -> None:
        offenders = [p.name for p in _leaf_files() if _imports_router(p.read_text(encoding="utf-8"), filename=p.name)]
        assert not offenders, (
            "src/teatree/hooks leaves must stay leaves: the router imports them, never the reverse. "
            f"These leaves carry a back-edge to hook_router: {offenders}. Move the shared helper into the "
            "leaf (or a deeper leaf both import) instead of importing the router back."
        )

    def test_at_least_one_leaf_scanned(self) -> None:
        # Guard against a path typo silently making the gate scan nothing.
        assert _leaf_files(), f"no hook leaves found under {_HOOKS_LEAF_DIR} — the gate would be vacuous"


class TestCheckerIsAntiVacuous:
    _CLEAN_LEAF = "from teatree.backends.slack.http import SlackHttpClient\n\n\ndef post() -> None:\n    pass\n"
    _TOPLEVEL_BACK_EDGE = "from hook_router import _helper\n\n\ndef post() -> None:\n    _helper()\n"
    _LAZY_BACK_EDGE = "def post() -> None:\n    from hook_router import _helper  # noqa: PLC0415\n\n    _helper()\n"
    _DOTTED_BACK_EDGE = "import hooks.scripts.hook_router as r\n\n\ndef post() -> None:\n    r.main()\n"

    def test_flags_top_level_back_edge(self) -> None:
        assert _imports_router(self._TOPLEVEL_BACK_EDGE) is True

    def test_flags_lazy_back_edge(self) -> None:
        assert _imports_router(self._LAZY_BACK_EDGE) is True

    def test_flags_dotted_back_edge(self) -> None:
        assert _imports_router(self._DOTTED_BACK_EDGE) is True

    def test_does_not_flag_clean_leaf(self) -> None:
        assert _imports_router(self._CLEAN_LEAF) is False
