"""Inertness fitness — the teams package is dead code until a future PR (#1838 PR#6).

PR#6 ships the team-role registry DARK: the loop, dispatch, and claim
execution paths must NOT import ``teatree.teams`` yet. This AST-level import
scan turns RED the moment any live path wires the registry in, so the
"ships inert, zero behaviour change" invariant cannot silently regress.
"""

import ast
from pathlib import Path

import teatree

_SRC_ROOT = Path(teatree.__file__).resolve().parent

# Every package whose modules drive the loop / dispatch / claim execution path.
# None of them may import the teams package while the feature ships dark.
_LIVE_PATH_PACKAGES = (
    "loop",
    "loops",
    "agents",
)
# The single claim chokepoint + its CLI entry.
_LIVE_PATH_MODULES = (
    "core/managers.py",
    "core/loop_lease_manager.py",
    "cli/loop.py",
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _LIVE_PATH_PACKAGES:
        files.extend((_SRC_ROOT / pkg).rglob("*.py"))
    files.extend(_SRC_ROOT / rel for rel in _LIVE_PATH_MODULES)
    return files


def _imports_teams(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "teatree.teams" or alias.name.startswith("teatree.teams.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "teatree.teams" or module.startswith("teatree.teams."):
                return True
    return False


class TestTeamsRegistryIsInert:
    def test_no_live_path_module_imports_the_teams_package(self) -> None:
        offenders = [str(p.relative_to(_SRC_ROOT)) for p in _python_files() if _imports_teams(p)]
        assert not offenders, f"teams package is wired into the live loop/claim path: {offenders}"

    def test_the_scan_actually_covers_files(self) -> None:
        # Guard the guard: if globbing found nothing the inertness test would
        # pass vacuously.
        assert len(_python_files()) > 5
