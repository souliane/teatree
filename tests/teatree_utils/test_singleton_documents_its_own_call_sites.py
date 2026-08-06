"""The module docstring must name only entry points that actually take the flock.

An operator auditing concurrency reads this docstring and stops there. Naming an
entry point that never calls :func:`singleton` tells them a guard exists where none
does — the same "absent signal read as a definite verdict" shape, at the doc layer.
"""

import ast
from pathlib import Path

from teatree.utils import singleton

_SRC = Path(singleton.__file__).resolve().parents[1]


def _modules_calling_singleton() -> set[str]:
    """Every module under ``src/teatree`` with a call to ``singleton(...)``."""
    callers: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "singleton":
                callers.add(path.name)
    return callers


class TestDocstringMatchesCallSites:
    def test_loop_tick_is_not_claimed_to_be_flock_wrapped(self) -> None:
        doc = singleton.__doc__ or ""
        tick_paragraph = doc[doc.index("loop tick") :]
        assert "LoopLease" in tick_paragraph
        assert "wraps its main loop" not in tick_paragraph

    def test_no_tick_module_actually_takes_the_flock(self) -> None:
        assert not {name for name in _modules_calling_singleton() if "tick" in name}

    def test_the_documented_flock_holders_do_call_it(self) -> None:
        callers = _modules_calling_singleton()
        assert "listen.py" in callers
        assert "overlay.py" in callers
