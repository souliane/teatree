"""Default-off zero-behaviour-change for the inert pane layer (#1838 PR#7a).

The whole PR ships dark: ``teams_enabled`` defaults False, and the pane FSM /
reaper / guardrails are referenced by NOTHING in the live loop / dispatch /
claim path. These tests pin the "zero behaviour change when disabled" invariant
at the seams a regression would surface: the config default, and the import
graph (no live-path module reaches the pane modules).
"""

import ast
from pathlib import Path

import teatree
from teatree.config import load_config

_SRC_ROOT = Path(teatree.__file__).resolve().parent

_LIVE_PATH_PACKAGES = ("loop", "loops", "agents")
_LIVE_PATH_MODULES = ("core/managers.py", "core/loop_lease_manager.py", "cli/loop.py")

# Every pane-layer module that must stay out of the live path while dark.
_PANE_MODULES = (
    "teatree.teams.panes",
    "teatree.teams.pane_reaper",
    "teatree.teams.guardrails",
)


def _live_path_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _LIVE_PATH_PACKAGES:
        files.extend((_SRC_ROOT / pkg).rglob("*.py"))
    files.extend(_SRC_ROOT / rel for rel in _LIVE_PATH_MODULES)
    return files


def _imports_any_pane_module(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in _PANE_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _PANE_MODULES or any(module.startswith(f"{m}.") for m in _PANE_MODULES):
                return True
    return False


class TestPaneLayerShipsDark:
    def test_teams_enabled_defaults_off(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_enabled is False

    def test_pane_budget_defaults_are_inert(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        settings = load_config(cfg).user
        # Conservative defaults: one pane, 30-minute idle threshold.
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30

    def test_no_live_path_module_imports_a_pane_module(self) -> None:
        offenders = [str(p.relative_to(_SRC_ROOT)) for p in _live_path_files() if _imports_any_pane_module(p)]
        assert not offenders, f"a pane-layer module is wired into the live loop/claim path: {offenders}"

    def test_the_scan_actually_covers_files(self) -> None:
        assert len(_live_path_files()) > 5
