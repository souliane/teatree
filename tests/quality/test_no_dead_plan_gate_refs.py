"""Fitness function: zero references to deleted plan-gate symbols.

The old TaskCreated/conversation-window plan-gate was replaced by the
structural FSM PlanArtifact gate (``Ticket.plan()`` + ``check_plan_artifact``
in ``src/teatree/core/gates/plan_gate.py``). Two symbols were deleted in that
migration:

- ``_agent_plan_gate_on_task_create_enabled``
- ``handle_enforce_plan_gate_on_task_create``

This test asserts ZERO references to either symbol remain anywhere in the
tracked tree (``src/``, ``hooks/``, ``docs/``, ``skills/``, ``tests/``).

RED on main (two dangling docstring references in ``hooks/scripts/hook_router.py``
exist before this cleanup). GREEN after the cleanup removes them.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("src", "hooks", "docs", "skills", "tests")
_SCAN_SUFFIXES = (".py", ".md", ".txt", ".yaml", ".yml", ".toml")

# This test file legitimately names the dead symbols in its docstring;
# exclude it from scanning so it does not flag itself.
_SELF = Path(__file__).resolve()

_DEAD_SYMBOLS: dict[str, re.Pattern[str]] = {
    "_agent_plan_gate_on_task_create_enabled": re.compile(r"_agent_plan_gate_on_task_create_enabled"),
    "handle_enforce_plan_gate_on_task_create": re.compile(r"handle_enforce_plan_gate_on_task_create"),
}


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCAN_DIRS:
        root = _REPO_ROOT / rel
        if not root.is_dir():
            continue
        for suffix in _SCAN_SUFFIXES:
            files.extend(p for p in root.rglob(f"*{suffix}") if p.resolve() != _SELF)
    return files


def _hits(pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    for path in _scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                out.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    return out


class TestNoDeadPlanGateRefs:
    @pytest.mark.parametrize("symbol", list(_DEAD_SYMBOLS))
    def test_deleted_symbol_has_zero_refs(self, symbol: str) -> None:
        hits = _hits(_DEAD_SYMBOLS[symbol])
        assert not hits, f"Deleted plan-gate symbol '{symbol}' still referenced ({len(hits)} hit(s)):\n" + "\n".join(
            hits
        )
