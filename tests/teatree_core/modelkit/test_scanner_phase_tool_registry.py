"""Scanner-produced phases have explicit tool entries and totality coverage."""

import ast
import importlib
from pathlib import Path
from types import ModuleType

from django.test import TestCase

import teatree.loop.scanners as scanners_pkg
from teatree.agents._runner_options import _disallowed_tools_for_phase
from teatree.core.modelkit.phase_tools import _TOOLS_BY_PHASE, tools_for_phase
from teatree.core.modelkit.phases import SCANNER_DISPATCHED_PHASES, SUBAGENT_BY_PHASE, normalize_phase

_SCANNER_DIR = Path(scanners_pkg.__file__).parent


def _resolve(node: ast.expr, module: ModuleType) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        return value if isinstance(value, str) else ""
    if isinstance(node, ast.Attribute):
        value = getattr(getattr(module, getattr(node.value, "id", ""), None), node.attr, None)
        return value if isinstance(value, str) else ""
    return ""


def _phase_tokens_in(module: ModuleType) -> set[str]:
    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    tokens = {
        _resolve(keyword.value, module)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "phase"
    }
    return tokens - {""}


def _scanner_dispatched_phase_tokens() -> set[str]:
    tokens: set[str] = set()
    for path in sorted(_SCANNER_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tokens |= _phase_tokens_in(importlib.import_module(f"{scanners_pkg.__name__}.{path.stem}"))
    return tokens


class TestScannerDispatchedPhasesCarryExplicitToolEntries(TestCase):
    def test_the_source_scan_actually_finds_scanner_phases(self) -> None:
        # Control: a derivation that silently found nothing would make every
        # assertion below vacuously green. Both a bare-literal phase
        # (``short_describe``) and an imported-constant phase
        # (``architectural_review``) must resolve, or the scan has a blind spot.
        found = _scanner_dispatched_phase_tokens()
        assert {"short_describe", "architectural_review", "dogfood_smoke"} <= found, found

    def test_every_scanner_written_phase_has_an_explicit_tool_entry(self) -> None:
        missing = sorted(
            phase for phase in _scanner_dispatched_phase_tokens() if normalize_phase(phase) not in _TOOLS_BY_PHASE
        )
        assert not missing, (
            f"scanner-dispatched phase(s) {missing} resolve through the deny-by-default "
            "read-only fallback, so Lane A denies Bash/Write/Edit on every dispatch"
        )

    def test_every_scanner_written_phase_is_a_producer_the_totality_lane_sees(self) -> None:
        # A phase is visible to the #3386 lane through EITHER route: the scanner
        # set, or a ``(role, phase)`` dispatch row. Absent from both, its tool
        # entry is unguarded and a later edit can silently drop it again.
        visible = {normalize_phase(phase) for phase in SCANNER_DISPATCHED_PHASES} | {
            normalize_phase(phase) for _role, phase in SUBAGENT_BY_PHASE
        }
        invisible = sorted(
            phase for phase in _scanner_dispatched_phase_tokens() if normalize_phase(phase) not in visible
        )
        assert not invisible, f"{invisible} are producers no totality lane covers"

    def test_eval_local_has_no_agent_tools(self) -> None:
        tools = tools_for_phase("eval_local")
        assert tools == frozenset()


class TestHeadlessSpawnKeepsItsShell(TestCase):
    def test_scanner_dispatched_shell_phase_is_not_denied_bash(self) -> None:
        assert "Bash" not in _disallowed_tools_for_phase("dogfood_smoke")

    def test_summariser_phase_is_never_granted_write_tools(self) -> None:
        # ``short_describe`` produces a <=40 char string; granting it the shell
        # would turn ~300 summarisation dispatches a day into autonomous
        # ticket-implementation agents.
        denied = _disallowed_tools_for_phase("short_describe")
        assert {"Write", "Edit", "Bash"} <= set(denied)
