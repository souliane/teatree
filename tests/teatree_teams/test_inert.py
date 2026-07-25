"""Pane-layer wiring fitness — only the sanctioned consumer reaches teams (#1838 PR#7b).

Exactly ONE live-path module reaches ``teatree.teams``:
``teatree.loop.scanners.pane_reaper`` (the idle-pane reaper scanner the
pane-reaper mini-loop dispatches). This AST-level import scan pins that; any
OTHER live-path module wiring the registry in turns it RED.

The maker claim path (``teatree.teams.pane_spawn``) has NO production caller at
all — no module-level import, no lazy one. So this scan currently ENFORCES the
pane-spawn half's dead state: wiring a maker consumer in would turn it red by
design, and re-specifying ``_SANCTIONED_TEAMS_CONSUMERS`` is part of that work.
Wire-vs-retire is an open owner decision (#3734).
"""

import ast
from pathlib import Path

import teatree

_SRC_ROOT = Path(teatree.__file__).resolve().parent

# Every package whose modules drive the loop / dispatch / claim execution path.
# None of them may import the teams package EXCEPT the sanctioned consumer below.
_LIVE_PATH_PACKAGES = (
    "loop",
    "loops",
    "agents",
)
# The single claim chokepoint + its CLI entry.
_LIVE_PATH_MODULES = (
    "core/managers.py",
    "core/loop_lease_manager.py",
    "cli/loop/app.py",
)
# The ONE sanctioned live-path consumer (#1838 PR#7b): the idle-pane reaper
# scanner the pane-reaper mini-loop dispatches. It may import teams; nothing else
# in the live path may.
_SANCTIONED_TEAMS_CONSUMERS = ("loop/scanners/pane_reaper.py",)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _LIVE_PATH_PACKAGES:
        files.extend((_SRC_ROOT / pkg).rglob("*.py"))
    files.extend(_SRC_ROOT / rel for rel in _LIVE_PATH_MODULES)
    sanctioned = {_SRC_ROOT / rel for rel in _SANCTIONED_TEAMS_CONSUMERS}
    return [p for p in files if p not in sanctioned]


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


class TestOnlyTheSanctionedConsumerWiresTeams:
    def test_no_unsanctioned_live_path_module_imports_the_teams_package(self) -> None:
        offenders = [str(p.relative_to(_SRC_ROOT)) for p in _python_files() if _imports_teams(p)]
        assert not offenders, f"an unsanctioned live-path module wires the teams package in: {offenders}"

    def test_the_sanctioned_consumer_does_import_teams(self) -> None:
        # Guard the guard: the allowlisted consumer must genuinely import teams,
        # else the allowlist is masking a non-existent edge (and a future move of
        # the import would silently widen the scan's blind spot).
        consumer = _SRC_ROOT / _SANCTIONED_TEAMS_CONSUMERS[0]
        assert _imports_teams(consumer)

    def test_the_scan_actually_covers_files(self) -> None:
        # Guard the guard: if globbing found nothing the scan would pass vacuously.
        assert len(_python_files()) > 5
