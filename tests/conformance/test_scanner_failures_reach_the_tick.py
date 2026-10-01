"""A scanner that failed must not read as a scanner that found nothing.

The dispatcher already isolates every scanner: ``teatree.loop.domain_jobs._run_job``
catches whatever ``scan()`` raises, logs it, and records it in the tick's
``report.errors`` — the statusline's ``scanner errors`` line and the ``WARN`` lines
``t3 loops tick`` prints. A ``scan()`` that wraps its own body in ``except Exception:
return []`` preempts that catcher, so a board reconcile, a question drain or a DM
re-delivery that fails on every tick reports ``0 signal(s)`` forever.

Scope: the pre-fix shape only — a ``try`` directly in a ``scan`` method's body whose
``Exception`` (or bare) handler returns an empty list. Per-item isolation inside a
loop, and the pre-migration ``(OperationalError, ProgrammingError)`` tolerance, are
not this shape and read clean.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT, src_modules


def _catches_everything(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(name, ast.Name) and name.id in {"Exception", "BaseException"} for name in names)


def _returns_empty_list(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.List) and not stmt.value.elts
        for stmt in handler.body
    )


def swallowing_scans(tree: ast.Module) -> list[str]:
    """``Class.scan:line`` for every whole-scan ``except Exception: return []`` in *tree*."""
    found = []
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or method.name != "scan":
                continue
            found.extend(
                f"{cls.name}.scan:{handler.lineno}"
                for stmt in method.body
                if isinstance(stmt, ast.Try)
                for handler in stmt.handlers
                if _catches_everything(handler) and _returns_empty_list(handler)
            )
    return found


def test_the_detector_recognises_the_swallow() -> None:
    source = (
        "class S:\n"
        "    def scan(self):\n"
        "        try:\n"
        "            return work()\n"
        "        except Exception:\n"
        "            return []\n"
    )
    assert swallowing_scans(ast.parse(source)) == ["S.scan:5"]


def test_per_item_isolation_and_the_premigration_tolerance_read_clean() -> None:
    source = (
        "class S:\n"
        "    def scan(self):\n"
        "        try:\n"
        "            rows = read()\n"
        "        except (OperationalError, ProgrammingError):\n"
        "            return []\n"
        "        for row in rows:\n"
        "            try:\n"
        "                handle(row)\n"
        "            except Exception:\n"
        "                continue\n"
        "        return []\n"
    )
    assert swallowing_scans(ast.parse(source)) == []


def test_no_scanner_swallows_its_own_failure() -> None:
    offenders = [
        f"{Path(path).relative_to(REPO_ROOT)}::{where}"
        for path, tree in src_modules()
        for where in swallowing_scans(tree)
    ]
    assert offenders == [], (
        "these scan() methods turn a failure into '0 signals'; let it raise so _run_job records it: "
        + ", ".join(offenders)
    )
