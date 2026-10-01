"""A mechanical handler that failed must not read as one that did its job.

``teatree.loop.tick_recovery._execute_mechanical`` already isolates every handler: it
catches whatever the handler raises, logs it, and records it in the tick's
``report.errors`` — the statusline's ``scanner errors`` line and the ``WARN`` lines
``t3 loops tick`` prints. A handler that wraps a step in ``except Exception`` and returns
preempts that catcher, so a backup, a snapshot refresh or a CI-eval heal that fails on
every tick is visible only in a log line.

Scope: a ``try`` directly in a registered handler's body whose ``Exception`` (or bare)
handler does not re-raise. Narrow catches, and isolation inside the helpers a handler
calls, are not this shape and read clean.
"""

import ast
import inspect
import textwrap
from collections.abc import Callable

from teatree.loop.mechanical import HANDLERS


def _catches_everything(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(name, ast.Name) and name.id in {"Exception", "BaseException"} for name in names)


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) for stmt in handler.body for node in ast.walk(stmt))


def swallowing_lines(function: ast.FunctionDef) -> list[int]:
    """Line of every top-level ``except Exception`` in *function* that does not re-raise."""
    return [
        handler.lineno
        for stmt in function.body
        if isinstance(stmt, ast.Try)
        for handler in stmt.handlers
        if _catches_everything(handler) and not _reraises(handler)
    ]


def _parsed(handler: Callable[..., object]) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    return function


def test_the_detector_recognises_the_swallow() -> None:
    source = "def h(payload):\n    try:\n        work()\n    except Exception:\n        log()\n        return\n"
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert swallowing_lines(function) == [4]


def test_a_narrow_catch_and_a_reraise_read_clean() -> None:
    source = (
        "def h(payload):\n"
        "    try:\n"
        "        row = load()\n"
        "    except Row.DoesNotExist:\n"
        "        return\n"
        "    try:\n"
        "        work(row)\n"
        "    except Exception:\n"
        "        release(row)\n"
        "        raise\n"
    )
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.FunctionDef)
    assert swallowing_lines(function) == []


def test_no_mechanical_handler_swallows_its_own_failure() -> None:
    offenders = [
        f"{zone} ({handler.__module__}.{handler.__name__}:{line})"
        for zone, handler in sorted(HANDLERS.items())
        for line in swallowing_lines(_parsed(handler))
    ]
    assert offenders == [], (
        "these handlers turn a failure into a log line; let it raise so _execute_mechanical records it: "
        + ", ".join(offenders)
    )
